"""Reach the existing cancellation path from schedule mutations in this process."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from agent.interrupt_scope import InterruptScope


class RunControl(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self.started_at = None
        self.delivery_completed = False
        self.scope = InterruptScope()
        self.local = True

    def set(self) -> None:
        super().set()
        self.scope.cancel("Cron job removed or paused", tool_reason="cron schedule cancelled")


def register_control(key: tuple) -> RunControl:
    """Caller holds scheduler._running_lock; registration precedes dispatch."""
    from cron import scheduler as sched

    control = RunControl()
    sched._running_cancel_events[key] = control
    return control


def release_unstarted_claim(job: dict) -> None:
    """Release only this cancelled attempt, without consuming repeat or outcome fields."""
    from cron import jobs

    owner = (job.get("fire_claim") or {}).get("by")
    if owner is None:
        return

    def apply(records, _index, current):
        if (current.get("fire_claim") or {}).get("by") == owner:
            current["fire_claim"] = None
            jobs.save_jobs(records)

    jobs._under_fire_fence(job["id"], lambda: jobs._with_job(job["id"], apply))


def defer_unstarted_fire(job: dict, reason: str) -> bool:
    """Put a scheduled fire that never reached the gateway owner back on the schedule.

    Releases only this attempt's claim and re-stamps its occurrence as an unclaimed
    ``pending_slot`` (one-shots refund their preclaim instead), recording no run outcome and
    no pause. The next due scan restores the slot once this process no longer runs the job,
    and the ordinary late / catch-up policy bounds the retries. False when the fire has no
    occurrence to restore (a manual run) or the claim is no longer this attempt's: the caller
    then fails the run as before, so nothing is dropped silently."""
    from cron import jobs
    from cron.job_run_retry import preserve_unstarted_run
    from cron.occurrences import pending_slot_stamp

    owner = (job.get("fire_claim") or {}).get("by")
    instant = job.get("_scheduled_instant")
    if owner is None or not instant:
        return False

    def apply(records, _index, current):
        if (current.get("fire_claim") or {}).get("by") != owner:
            return False
        now = jobs._hermes_now()
        kind = (current.get("schedule") or {}).get("kind")
        if kind in {"cron", "interval"}:
            current["pending_slot"] = pending_slot_stamp(instant, now)
        elif kind == "once":
            preserve_unstarted_run(current, now.isoformat())
            if current.get("run_claim") is not None:
                current["run_claim"] = None
        else:
            return False
        current["fire_claim"] = None
        current["last_fire_error"] = {"at": now.isoformat(), "detail": f"Deferred, not started: {reason}"[:500]}
        jobs.save_jobs(records)
        return True

    return bool(jobs._under_fire_fence(job["id"], lambda: jobs._with_job(job["id"], apply, False)))


def owns_terminal_claim(job_id: str, owner: str) -> bool:
    """Rejected/missing claims cannot write a run; a stopped own claim still needs release."""
    from cron import scheduler as sched
    from cron.jobs import get_job, is_job_runnable

    if sched.heartbeat_fire_claim(job_id, expected_owner=owner):
        return True
    job = get_job(job_id)
    return bool(job and not is_job_runnable(job)
                and (job.get("fire_claim") or {}).get("by") == owner)


def rearm_unstarted_control(job: dict, control: RunControl, key: tuple) -> RunControl:
    """A pause/resume while queued must not cancel the resumed occurrence."""
    from cron import jobs, scheduler as sched

    current = jobs.get_job(job["id"])
    if control.started_at is None and current is not None and jobs.is_job_runnable(current):
        with sched._running_lock:
            if sched._running_cancel_events.get(key) is control:
                return register_control(key)
    return control


def start_control(job_id: str) -> None:
    from cron import scheduler as sched

    with sched._running_lock:
        control = sched._running_cancel_events.get(sched._inflight_key(job_id))
        if control is not None:
            control.started_at = datetime.now(timezone.utc).isoformat()


def note_completed_delivery(job_id: str) -> None:
    from cron import scheduler as sched

    with sched._running_lock:
        control = sched._running_cancel_events.get(sched._inflight_key(job_id))
        if control is not None:
            control.delivery_completed = True


def schedule_cancelled(job_id: str, owner: str | None) -> bool:
    from cron import scheduler as sched
    from cron.jobs import get_job, is_job_runnable

    with sched._running_lock:
        control = sched._running_cancel_events.get(sched._inflight_key(job_id))
        if control is not None and control.is_set():
            return True
    current = get_job(job_id)
    return current is None or (not is_job_runnable(current)
                              and (current.get("fire_claim") or {}).get("by") == owner)


def record_cancelled_run(job_id: str, owner: str, error: str) -> bool:
    """Keep the audit outcome and failure streak; started one-shots retain their dispatch."""
    from cron import jobs

    def apply(records, _index, current):
        if (current.get("fire_claim") or {}).get("by") != owner:
            return False
        streak = current.get("failure_streak", 0)
        jobs._record_run_outcome(current, False, error, None, None, jobs._hermes_now().isoformat())
        current["failure_streak"] = streak
        jobs.save_jobs(records)
        return True

    return jobs._under_fire_fence(job_id, lambda: jobs._with_job(job_id, apply, False))


def running_job_state(job: dict) -> dict:
    from cron import scheduler as sched
    from cron.jobs import FIRE_CLAIM_TTL_SECONDS, _claim_is_live, _hermes_now
    from cron.executions import live_inflight_execution

    key = sched._inflight_key(str(job.get("id") or "unknown"))
    with sched._running_lock:
        local = key in sched._running_job_ids or key in sched._running_fire_owners
        control = sched._running_cancel_events.get(key)
        started = control.started_at if control is not None else None
    claim = job.get("fire_claim") or {}
    live_claim = _claim_is_live(claim, _hermes_now(), FIRE_CLAIM_TTL_SECONDS)
    detached = live_inflight_execution(str(job.get("id") or ""))
    if detached is not None and detached.get("owner_kind") == "detached":
        return {
            "was_running": True,
            "run_started_at": detached.get("started_at") or detached.get("claimed_at"),
            "run_id": detached.get("id"),
            "interrupted": False,
            "delivery_completed": False,
            "_detached_execution": detached,
        }
    queued = local and control is not None and control.local and started is None
    return {
        "was_running": not queued and (bool(started) or live_claim),
        "run_started_at": (
            None if queued else started or (claim.get("started_at", claim.get("at")) if live_claim else None)),
        "run_id": None,
        "interrupted": False,
        "delivery_completed": bool(control and control.delivery_completed),
    }


def interrupt_job_run(job: dict) -> dict:
    """Report a live remote claim honestly; an Event cannot stop another process."""
    from cron import scheduler as sched
    from cron.jobs import self_removal_delivery_allowed
    from cron.detached_authority import cancel_detached

    result = running_job_state(job)
    detached = result.pop("_detached_execution", None)
    if detached is not None and detached.get("owner_kind") == "detached":
        cancel_detached(detached)
        result["interrupted"] = True
        result["warning"] = (
            "A detached run was in progress and cancellation was recorded. "
            "Side effects already performed cannot be undone; read back the target before retrying.")
        return result
    with sched._running_lock:
        control = sched._running_cancel_events.get(sched._inflight_key(job["id"]))
    if control is not None and control.local and not self_removal_delivery_allowed(job["id"]):
        control.set()
        result["interrupted"] = result["was_running"] and not result["delivery_completed"]
    if result["delivery_completed"]:
        result["warning"] = (
            "Delivery completed before the schedule was cancelled; it was not interrupted. "
            "Side effects cannot be undone; read back the target before retrying.")
    elif result["was_running"]:
        result["warning"] = (
            "A run was in progress. Interruption is requested, not proof of rollback or tool "
            "completion. Side effects already performed cannot be undone; read back the target "
            "before retrying."
            if result["interrupted"] else
            "A run was in progress but was not interrupted in this process. It may still perform "
            "side effects; read back the target before retrying. Side effects cannot be undone.")
    return result
