"""One-shot grace, scheduler-stall catch-up and durable missed-run accounting."""

import logging
import math
import os

logger = logging.getLogger("cron.jobs")


def _stall_covers(d) -> bool:
    from cron import jobs

    fields = jobs._read_marker_fields("ticker_heartbeat")
    try:
        epoch, pid = float(fields[0]), int(fields[1])
    except (IndexError, ValueError):
        return False
    # The beat is written AFTER a tick (and at startup). A same-process beat before the
    # deadline means this is its first opportunity to observe the expired slot. A fresh
    # startup beat after the deadline or another process's beat proves no such continuity.
    return (
        pid == os.getpid() and math.isfinite(epoch) and epoch > 0
        and epoch <= d.scan.now.timestamp()
        and epoch <= d.next_run_dt.timestamp() + jobs.ONESHOT_GRACE_SECONDS
    )


def _record_miss(d) -> str:
    from cron import executions, incidents
    from cron.occurrences import scheduled_instant

    row = executions.latest_execution(d.job["id"])
    # A save/incident failure leaves the job intact. Retry the accounting without inventing
    # a second missed attempt or changing the original scan time.
    if not (row and row["source"] == "scheduler-missed"
            and row["status"] == "failed"
            and row["scheduled_instant"] == scheduled_instant(d.next_run)):
        error = (
            f"Missed one-shot: scheduled for {d.next_run}; due scan at {d.scan.now.isoformat()} "
            "was beyond the 120-second grace window without scheduler continuity. Job did not run."
        )
        row = executions.create_execution(
            d.job["id"], source="scheduler-missed", scheduled_instant=d.next_run)
        row = executions.finish_execution(row["id"], success=False, error=error)
        if row is None:
            raise RuntimeError("Missed one-shot execution could not be terminalized")
    error = row["error"]
    # Unlike the delivery helper's best-effort upsert, this must succeed before deletion.
    incidents.upsert_incident(d.job["id"], error, job_name=d.job.get("name"))
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
    if _stall_covers(d):
        return False
    if not (claim or d.job.get("fire_claim")):
        error = _record_miss(d)
        jobs._write_missed_oneshot_diagnostic(d.job, d.next_run)
        d.scan.retire(d.job["id"])
        d.scan.on_saved.append(lambda job=d.job: jobs.record_cron_missed(job))
        d.scan.missed.append((d.job, error))
        logger.error("Job %s: %s", d.job["id"], error)
    return True


def deliver_misses(missed, *, adapters, loop) -> None:
    """Use the normal failure route only after the jobs save and outside its lock."""
    from cron import scheduler

    for job, error in missed:
        try:
            scheduler._deliver_crash_failure(job, error, adapters=adapters, loop=loop)
        except Exception:
            # The execution and incident remain visible even if notification itself fails.
            logger.exception("Failed delivering missed one-shot notice for job %s", job["id"])
