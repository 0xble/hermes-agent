"""Explicit pre-agent contention outcome; retries remain owned by the existing job store."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json


@dataclass(frozen=True)
class DeferredRun:
    reason: str
    retry_after_seconds: int


def parse_deferral(output: str) -> DeferredRun | None:
    """Only the last stdout line is control data; callers must first verify exit zero."""
    lines = output.strip().splitlines()
    if not lines:
        return None
    try:
        value = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(value, dict) or "defer" not in value:
        return None
    request = value["defer"]
    if not isinstance(request, dict):
        raise ValueError("Invalid pre-run defer request")
    delay, reason = request.get("retry_after_seconds"), request.get("reason")
    if (type(delay) is not int or not 1 <= delay <= 3600
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 500):
        raise ValueError("Pre-run defer requires a reason (1–500 characters) and retry_after_seconds (1–3600 integer)")
    return DeferredRun(reason.strip(), delay)


def reconcile_pending(job: dict) -> None:
    """Jobs.json is the durable pre-agent proof across a crash between the two stores.

    Caller holds the job lock and fire fence (or the job lock during recovery). A later claim
    seals this exact old attempt before removing its proof; it never adopts the old run identity.
    """
    from cron.executions import _finish_deferred_execution

    pending = job.get("deferred_run")
    if pending:
        _finish_deferred_execution(job["id"], pending["execution_id"], pending["reason"])


def finish_deferred_run(job: dict, result: DeferredRun, execution_id: str, owner: str | None) -> bool:
    """Owner-fenced pre-agent finalization, without normal run accounting or delivery."""
    from cron import jobs

    if not owner:
        raise RuntimeError("Pre-run deferral requires a fire claim")

    def apply(records, _i, current):
        claim = current.get("fire_claim")
        if not isinstance(claim, dict) or claim.get("by") != owner:
            return False
        # Validate ledger ownership before publishing durable proof that this attempt deferred.
        from cron.executions import get_execution, _PROCESS_ID
        import os
        execution = get_execution(execution_id)
        if (not execution or execution["job_id"] != job["id"]
                or execution["status"] != "running" or execution["process_id"] != _PROCESS_ID
                or execution["pid"] != os.getpid()):
            return False
        now = jobs._hermes_now()
        retry_at = (now.astimezone(timezone.utc) + timedelta(seconds=result.retry_after_seconds)).astimezone(now.tzinfo).isoformat()
        current["deferred_run"] = {"execution_id": execution_id, "reason": result.reason,
                                   "retry_at": retry_at,
                                   "scheduled_instant": job.get("_scheduled_instant")}
        current["next_run_at"] = retry_at
        # Finite one-shots reserve their budget before running even the pre-check. No agent
        # started: refund only this exact owner's dispatch reservation.
        repeat = current.get("repeat") or {}
        if current.get("schedule", {}).get("kind") == "once" and (repeat.get("times") or 0) > 0:
            repeat["completed"] = max(0, repeat.get("completed", 0) - 1)
        current["fire_claim"] = None
        current.pop("run_claim", None)
        current.pop("pending_slot", None)
        jobs.save_jobs(records)
        reconcile_pending(current)
        return True

    return jobs._under_fire_fence(job["id"], lambda: jobs._with_job(job["id"], apply, False))


def pending_due(job: dict, now) -> bool | None:
    """A deferred occurrence is pending work, not a missed calendar slot or stale error."""
    from cron.jobs import _claim_is_live, FIRE_CLAIM_TTL_SECONDS

    pending = job.get("deferred_run")
    if not pending:
        return None
    if _claim_is_live(job.get("fire_claim"), now, FIRE_CLAIM_TTL_SECONDS):
        return False
    return datetime.fromisoformat(pending["retry_at"]) <= now
