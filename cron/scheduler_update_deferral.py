"""Scheduling bookkeeping for a proved-unsubmitted gateway update fence, not a run outcome."""
from __future__ import annotations

import json


def defer_unadmitted_job(job: dict, execution_id: str) -> bool:
    """Refund only our one-shot preclaim; recurring jobs keep their already-advanced schedule.

    The authority calls this path only without an admission journal or submit attempt. Never
    manufacture a successful audit result (which would dedupe the one-shot) or recover an incident.
    """
    from cron import jobs
    from cron.executions import discard_unadmitted_execution
    from cron.scheduler_authority import journal_path

    if journal_path(job["id"], execution_id).exists():
        raise RuntimeError("Cannot defer a journaled cron admission")
    owner = (job.get("fire_claim") or {}).get("by")

    def apply(records, _index, current):
        if not owner or (current.get("fire_claim") or {}).get("by") != owner:
            return False
        current["fire_claim"] = None
        # A one-shot run claim belongs to the same occurrence protected by the fire fence.
        current["run_claim"] = None
        current["last_skipped_at"] = jobs._hermes_now().isoformat()
        current["last_skip_reason"] = "update_paused"
        if current.get("schedule", {}).get("kind") == "once":
            repeat = current.get("repeat") or {}
            if repeat.get("times") is not None and repeat["times"] > 0:
                repeat["completed"] = max(0, repeat.get("completed", 0) - 1)
            # Tie the exemption from missed-one-shot retirement to this exact arm. Schedule
            # edits/re-arm change the instant; explicit deadline policy still runs first.
            current["update_deferred_slot"] = current.get("next_run_at")
        jobs.save_jobs(records)
        return True

    def locked():
        deferred = jobs._with_job(job["id"], apply, False)
        # Release the audit attempt even if the fire owner changed: the pre-admission refusal
        # proves THIS attempt cannot have run; it grants no authority over a replacement claim.
        if not discard_unadmitted_execution(execution_id):
            raise RuntimeError("Cron update deferral lost execution ownership")
        return deferred

    return jobs._under_fire_fence(job["id"], locked)


def handle_admission_exception(job: dict, execution_id: str, error: BaseException) -> bool | None:
    """Return a non-run outcome without using the ordinary failure/notice lane."""
    from cron import scheduler
    from cron.executions import give_up_execution
    from cron.scheduler_authority import CronExecutionDeferred, CronExecutionUnknown, pause_for_unknown

    if isinstance(error, CronExecutionDeferred):
        defer_unadmitted_job(job, execution_id)
        # Set on the incoming payload, not _run_one_job_body's copy: the worker's
        # ready-ack publisher runs after run_one_job returns with this dictionary.
        job["_update_paused_deferral"] = True
        scheduler.logger.info("Cron %s deferred before admission: update_paused", job["id"])
        return True  # processed, not success or incident recovery
    if isinstance(error, CronExecutionUnknown):
        pause_for_unknown(job["id"], execution_id, str(error))
        give_up_execution(execution_id, reason=str(error))
        scheduler.logger.error("Cron %s paused with unverified admission: %s", job["id"], error)
        return False
    return None


def publish_worker_deferral(ack_path, job: dict) -> None:
    """The existing exact-worker acknowledgement carries a non-success exit receipt."""
    from utils import atomic_json_write
    if job.get("_update_paused_deferral"):
        receipt = json.loads(ack_path.read_text(encoding="utf-8-sig"))
        receipt["deferral_reason"] = "update_paused"
        atomic_json_write(ack_path, receipt, mode=0o600, fsync_dir=True)


def worker_deferred(process, execution_id: str, handoff_files) -> bool:
    """Only the reaped, exact worker's structural receipt can replace a missing audit row."""
    from cron.executions import get_execution
    if not handoff_files:
        return False
    try:
        exited_cleanly = process.poll() == 0
    except (AttributeError, OSError):
        return False  # failed observation must not mask the waiter's original error
    if not exited_cleanly or get_execution(execution_id) is not None:
        return False
    for path in handoff_files:
        if path.name != f"{execution_id}.ready":
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if receipt.get("execution_id") == execution_id and receipt.get("deferral_reason") == "update_paused":
            return True
    return False
