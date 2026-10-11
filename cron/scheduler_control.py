"""Reach the existing cancellation path from schedule mutations in this process."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from agent.interrupt_scope import InterruptScope


class RunControl(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self.started_at = datetime.now(timezone.utc).isoformat()
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


def running_job_state(job: dict) -> dict:
    from cron import scheduler as sched
    from cron.jobs import FIRE_CLAIM_TTL_SECONDS, _claim_is_live, _hermes_now

    key = sched._inflight_key(str(job.get("id") or "unknown"))
    with sched._running_lock:
        local = key in sched._running_job_ids or key in sched._running_fire_owners
        started = sched._running_since.get(key)
        control = sched._running_cancel_events.get(key)
    claim = job.get("fire_claim") or {}
    live_claim = _claim_is_live(claim, _hermes_now(), FIRE_CLAIM_TTL_SECONDS)
    return {
        "was_running": local or live_claim,
        "run_started_at": (
            datetime.fromtimestamp(started, timezone.utc).isoformat() if started is not None
            else claim.get("started_at", claim.get("at")) if live_claim
            else control.started_at if local and control is not None else None),
        "interrupted": False,
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
        result["was_running"] = result["interrupted"] = True
    if result["was_running"]:
        result["warning"] = (
            "A run was in progress. Interruption is requested, not proof of rollback or tool "
            "completion. Side effects already performed cannot be undone; read back the target "
            "before retrying."
            if result["interrupted"] else
            "A run was in progress but was not interrupted in this process. It may still perform "
            "side effects; read back the target before retrying. Side effects cannot be undone.")
    return result
