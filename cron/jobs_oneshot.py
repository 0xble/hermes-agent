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


def _record_miss(d) -> str:
    from cron import executions, incidents
    error = (
        f"Missed one-shot: scheduled for {d.next_run}; due scan at {d.scan.now.isoformat()} "
        "was beyond the 120-second grace window without scheduler continuity. Job did not run."
    )
    row = executions.record_missed_execution(
        d.job["id"], scheduled_instant=d.next_run, error=error)
    error = row["error"]
    # Match the durable retirement outbox's incident signature. Store failure must
    # keep the job for retry, rather than deleting it with only best-effort visibility.
    scheduled = (d.job.get("schedule") or {}).get("run_at") or d.next_run
    incidents.upsert_incident(
        d.job["id"],
        f"missed-oneshot: one-shot {d.job['id']} scheduled at {scheduled}; "
        "Job not run; recreate to run.", job_name=d.job.get("name"))
    return error


def retire_expired_oneshot(d) -> bool:
    """Skip expired claims, catch up scheduler/store stalls, or durably account for a miss."""
    from cron import jobs

    if jobs._elapsed_seconds(d.scan.now, d.next_run_dt) <= jobs.ONESHOT_GRACE_SECONDS:
        return False
    if jobs.store_health.outage_covers(
            jobs._current_cron_store().cron_dir, d.next_run_dt.timestamp(), jobs.ONESHOT_GRACE_SECONDS):
        return False
    claim = d.job.get("run_claim")
    # The existing interruption stamp keeps a catch-up eligible if dispatch could not start.
    if isinstance(claim, dict) and claim.get("outage") and not d.job.get("fire_claim"):
        return False
    # A consumed dispatch is interrupted, not a never-started scheduler miss.
    # Preserve main's partial-effects diagnostic and never replay it after a stall.
    if (d.job.get("repeat") or {}).get("completed", 0) > 0:
        if not (claim or d.job.get("fire_claim")):
            jobs._write_retired_oneshot_diagnostic(d.job, d.next_run)
            d.scan.retire(d.job["id"])
            d.scan.on_saved.append(lambda job=d.job: jobs.record_cron_missed(job))
        return True
    if _stall_covers(d):
        return False
    if not (claim or d.job.get("fire_claim")):
        error = _record_miss(d)
        # The existing outbox commits the owner notice and incident before deletion.
        # Its durable receipt survives a jobs-save failure without a duplicate send.
        jobs._write_retired_oneshot_diagnostic(d.job, d.next_run)
        d.scan.retire(d.job["id"])
        d.scan.on_saved.append(lambda job=d.job: jobs.record_cron_missed(job))
        logger.error("Job %s: %s", d.job["id"], error)
    return True
