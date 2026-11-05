"""One-shot grace, scheduler-stall catch-up and durable missed-run accounting."""

import logging
import math
import os
import uuid
from datetime import datetime

logger = logging.getLogger("cron.jobs")
_SCAN_PROCESS_ID = uuid.uuid4().hex


def record_due_scan(now: datetime) -> None:
    """Persist this profile's scan instant, independent of cycle-completion heartbeats."""
    from cron.jobs import _write_marker

    _write_marker("ticker_due_scan", f"{now.timestamp()} {os.getpid()} {_SCAN_PROCESS_ID}", ".scan_")


def _stall_covers(d) -> bool:
    from cron import jobs

    fields = jobs._read_marker_fields("ticker_due_scan")
    try:
        epoch, pid, process_id = float(fields[0]), int(fields[1]), fields[2]
    except (IndexError, ValueError):
        return False
    # Only a prior scan in this process proves continuity. Provider heartbeats
    # may be refreshed minutes after this profile was scanned (including stalls
    # in another multiplexed profile). The nonce also rejects recycled PIDs.
    return (
        pid == os.getpid() and process_id == _SCAN_PROCESS_ID
        and math.isfinite(epoch) and epoch > 0
        and epoch <= d.scan.now.timestamp()
        and epoch <= d.next_run_dt.timestamp() + jobs.ONESHOT_GRACE_SECONDS
    )


def _record_miss(d, cause: str) -> str:
    from cron import executions, incidents
    from cron.oneshot_retirement import held_outcome
    error = (
        f"Missed one-shot: scheduled for {d.next_run}; due scan at {d.scan.now.isoformat()} "
        f"was beyond the 120-second grace window ({cause}). "
        "Job did not run. Payload kept for an owner decision; no automatic replay."
    )
    row = executions.record_missed_execution(
        d.job["id"], scheduled_instant=d.next_run, error=error)
    error = row["error"]
    # Match the durable retirement outbox's incident signature. Store failure must
    # keep the job for retry, rather than deleting it with only best-effort visibility.
    scheduled = (d.job.get("schedule") or {}).get("run_at") or d.next_run
    incidents.upsert_incident(
        d.job["id"],
        f"missed-oneshot-held: one-shot {d.job['id']} scheduled at {scheduled}; "
        f"{held_outcome(d.job)}", job_name=d.job.get("name"))
    return error


def occurrence_started(job: dict, *, now: datetime | None = None) -> bool:
    """Evidence belongs to this arm, never to the job's historical last_run_at.

    Re-arm resets the budget and fire claim. A due-scan run claim or a queued /
    missed ledger row is only admission, not evidence of possible side effects.
    """
    from cron import jobs
    from cron.executions import _transaction
    from cron.occurrences import scheduled_instant

    if ((job.get("repeat") or {}).get("completed", 0) > 0
            or jobs._claim_is_live(job.get("fire_claim"), now or jobs._hermes_now(),
                                   jobs.FIRE_CLAIM_TTL_SECONDS)):
        return True
    instant = scheduled_instant(job.get("next_run_at") or (job.get("schedule") or {}).get("run_at"))
    if instant is None:
        return False
    with _transaction() as conn:
        return conn.execute(
            "SELECT 1 FROM executions WHERE job_id=? AND scheduled_instant=? "
            "AND started_at IS NOT NULL LIMIT 1", (str(job["id"]), instant),
        ).fetchone() is not None


def occurrence_finished(job: dict) -> bool:
    """Completion must also belong to this arm, not a retained history stamp."""
    from cron.occurrences import completed_occurrence, scheduled_instant

    if job.get("state") == "completed":
        return True
    instant = scheduled_instant(job.get("next_run_at") or (job.get("schedule") or {}).get("run_at"))
    return instant is not None and occurrence_started(job) and completed_occurrence(job, instant)


def retire_expired_oneshot(d) -> bool:
    """Keep overdue never-started payloads held; continuity is provenance, not send approval."""
    from cron import jobs
    from cron.oneshot_retirement import queue_retirement_alert

    if jobs._elapsed_seconds(d.scan.now, d.next_run_dt) <= jobs.ONESHOT_GRACE_SECONDS:
        return False
    covered = jobs.store_health.outage_covers(
        jobs._current_cron_store().cron_dir, d.next_run_dt.timestamp(), jobs.ONESHOT_GRACE_SECONDS)
    claim = d.job.get("run_claim")
    fire_live = jobs._claim_is_live(d.job.get("fire_claim"), d.scan.now, jobs.FIRE_CLAIM_TTL_SECONDS)
    # An outage-covered admission that never acquired a live fire claim never started.
    # Keep its payload, but do not treat recovery as permission for a stale send.
    if isinstance(claim, dict) and claim.get("outage") and not fire_live:
        d.scan.persist(d.job["id"], run_claim=None)
        d.job["run_claim"] = claim = None
    in_flight = (fire_live
                 or jobs._claim_is_live(claim, d.scan.now, jobs._oneshot_run_claim_ttl_seconds())
                 or jobs._job_running_in_this_process(d.job["id"]))
    # A consumed dispatch is interrupted, not a never-started scheduler miss.
    # Dead/expired claims cannot pin its record, but local live runners must keep it.
    if occurrence_started(d.job, now=d.scan.now):
        if not in_flight:
            jobs._write_retired_oneshot_diagnostic(d.job, d.next_run)
            d.scan.retire(d.job["id"])
            d.scan.on_saved.append(lambda job=d.job: jobs.record_cron_missed(job))
        return True
    if not in_flight:
        cause = ("scheduler scan stalled with continuity" if _stall_covers(d) else
                 "store outage covered run time" if covered else "without scheduler continuity")
        error = _record_miss(d, cause)
        # Match #1068's fail-closed policy: prompts and scripts can send to a
        # person even with deliver=local, so do not guess safety from delivery.
        queue_retirement_alert(d.job, error, "missed-oneshot-held", now=d.scan.now)
        try:
            jobs.save_job_output(d.job["id"], error + "\nThis one-shot was not deleted.")
        except OSError:
            logger.warning("Could not save held one-shot diagnostic for %s", d.job["id"])
        fields = dict(enabled=False, state="missed", missed_at=d.scan.now.isoformat(), missed_reason=cause)
        d.scan.persist(d.job["id"], **fields)
        d.job.update(fields)
        d.scan.on_saved.append(lambda job=d.job: jobs.record_cron_missed(job))
        logger.error("Job %s: %s", d.job["id"], error)
    return True
