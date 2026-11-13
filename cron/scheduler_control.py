"""Reach the existing cancellation path from schedule mutations in this process."""
from __future__ import annotations

import threading
from datetime import UTC, datetime

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


def _occurrence_superseded(claimed: dict, current: dict, *, paused_by_recovery: bool = False) -> bool:
    """True when an operator schedule or lifecycle edit landed after this fire was claimed.

    ``update_job`` drops ``pending_slot`` for exactly these fields because the edit supersedes
    any occurrence the dispatcher left unclaimed. A claimed-but-never-started fire is the same
    occurrence, so restoring it would revive the old slot and overwrite the operator's
    ``next_run_at`` (or, for a one-shot, the edited run time). Nothing between the claim and a
    pre-submit deferral legitimately rewrites these fields, so any difference is an edit.

    The one exception is the uncertainty pause this fire's own recovery recorded: it owns
    ``enabled``/``state`` and the pause markers, so it is judged as the claimed lifecycle."""
    from cron.jobs import is_job_runnable

    if paused_by_recovery:
        current = dict(current, enabled=claimed.get("enabled"), state=claimed.get("state"),
                       paused_at=None, paused_reason=None)
    if not is_job_runnable(current):
        return True
    return any(current.get(key) != claimed.get(key) for key in _OCCURRENCE_FIELDS)


# The fields whose rewrite ``update_job`` treats as superseding an unclaimed occurrence.
_OCCURRENCE_FIELDS = ("schedule", "timezone", "next_run_at", "enabled", "state")


def defer_unstarted_fire(job: dict, reason: str, *, pause: dict | None = None) -> str | None:
    """Restore a proven unstarted occurrence under its exact fire-owner fence.

    Shared by pre-submit deferral and detached pre-body recovery: no run outcome, pause, or
    failure-streak write. Releases only this attempt's claim; recurring fires restore the
    original occurrence as an unclaimed ``pending_slot``, while one-shots refund the preclaim
    and refresh their retry instant through ``preserve_unstarted_run``. The due scan enforces
    ordinary catch-up limits. A ``pause`` this fire's recovery recorded is lifted with the
    restore. Returns ``"deferred"``.

    When an operator schedule or lifecycle edit superseded the occurrence while the handshake
    stalled, the edit already chose what runs next: the claim is released (a one-shot's
    preclaim refunded) with no slot restored and the edited ``next_run_at`` untouched. Returns
    ``"superseded"``.

    None when the fire has no occurrence to restore (a manual run), the claim is no longer
    this attempt's, or the fire fence refused: nothing was restored or released, so the
    caller must keep or fail the run visibly rather than drop it."""
    from cron import jobs
    from cron.job_run_retry import preserve_unstarted_run
    from cron.occurrences import pending_slot_stamp

    owner = (job.get("fire_claim") or {}).get("by")
    instant = job.get("_scheduled_instant")
    if not owner or not instant:
        return None

    def apply(records, _index, current):
        if (current.get("fire_claim") or {}).get("by") != owner:
            return None
        paused_by_recovery = (
            isinstance(pause, dict)
            and (current.get("paused_at"), current.get("paused_reason"))
            == (pause.get("paused_at"), pause.get("reason"))
        )
        now = jobs._hermes_now()
        kind = (current.get("schedule") or {}).get("kind")
        if _occurrence_superseded(job, current, paused_by_recovery=paused_by_recovery):
            # The recovery pause, if any, is lifted when its journal retires (resume_job then
            # derives the next run from the edited schedule).
            if (job.get("schedule") or {}).get("kind") == "once":
                _refund_oneshot_preclaim(current)
            outcome, detail = "superseded", f"Not started; superseded by an operator edit: {reason}"
        elif kind in {"once", "cron", "interval"}:
            if paused_by_recovery:
                jobs._activate_job_record(current)
            if kind == "once":
                # #1281: the one-shot's retry instant is refreshed; no pending slot.
                preserve_unstarted_run(current, now.isoformat())
            else:
                current["pending_slot"] = pending_slot_stamp(instant, now)
            outcome, detail = "deferred", f"Deferred, not started: {reason}"
        else:
            return None
        if current.get("run_claim") is not None:
            current["run_claim"] = None
        current["fire_claim"] = None
        current["last_fire_error"] = {"at": now.isoformat(), "detail": detail[:500]}
        jobs.save_jobs(records)
        return outcome

    # ``_under_fire_fence`` answers False when the fence is busy: nothing was written.
    return jobs._under_fire_fence(job["id"], lambda: jobs._with_job(job["id"], apply, None)) or None


def _refund_oneshot_preclaim(current: dict) -> None:
    """Undo ``claim_dispatch``'s increment so an edited one-shot can still fire at its new time."""
    repeat = current.get("repeat") or {}
    times = repeat.get("times")
    if times is not None and times > 0 and repeat.get("completed", 0) > 0:
        repeat["completed"] -= 1


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
            control.started_at = datetime.now(UTC).isoformat()


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

    key = sched._inflight_key(str(job.get("id") or "unknown"))
    with sched._running_lock:
        local = key in sched._running_job_ids or key in sched._running_fire_owners
        control = sched._running_cancel_events.get(key)
        started = control.started_at if control is not None else None
    claim = job.get("fire_claim") or {}
    live_claim = _claim_is_live(claim, _hermes_now(), FIRE_CLAIM_TTL_SECONDS)
    queued = local and control is not None and control.local and started is None
    return {
        "was_running": not queued and (bool(started) or live_claim),
        "run_started_at": (
            None if queued else started or (claim.get("started_at", claim.get("at")) if live_claim else None)),
        "interrupted": False,
        "delivery_completed": bool(control and control.delivery_completed),
    }


def interrupt_job_run(job: dict) -> dict:
    """Report a live remote claim honestly; an Event cannot stop another process."""
    from cron import scheduler as sched
    from cron.jobs import self_removal_delivery_allowed

    result = running_job_state(job)
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
