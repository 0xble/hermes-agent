"""A requested graceful restart waits for the gateway to go idle, serving normally meanwhile.

SIGUSR1, ``/restart``, ``hermes gateway restart`` and the updater's ``pause-for-update`` used to put
the gateway in ``draining`` at once: every new turn, continuation, process/watch notice and
background-delegation completion was refused ``runtime_draining`` for the whole after-turn wait.
Parents could not hear back from their children while it ran.

Now the restart is "restart when idle", bounded: nothing is refused while the wait runs, and the
restart happens at the first moment no work is in flight. The wait itself stays bounded
(``GatewayShutdownMixin._await_active_work_before_restart``): turns wait at most
``agent.restart_after_turn_timeout`` and background delegations at most
``gateway.restart_delegation_timeout``, after which ``stop()`` drains what is left, so one stuck
unit cannot hold a restart forever. Admission closes when the wait ends. An explicit force
(``pause-for-update`` with ``{"force": true}``) ends the wait and stops live work at once; SIGTERM
keeps its immediate-stop meaning.

This module holds the parts the wait and its callers share: the background units it names, the
force, and the update pause's adoption of a restart that is already waiting.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

logger = logging.getLogger("gateway.run")


def delegation_units(now: float) -> list[dict]:
    """One unit per live background delegation, with the parent session it reports back to."""
    from tools.async_delegation import _LIVE_STATES, list_async_delegations
    units = []
    for record in list_async_delegations():
        if record.get("status") not in _LIVE_STATES:
            continue
        unit = {"kind": "delegation", "delegation_id": record.get("delegation_id"),
                "goal": str(record.get("goal") or "")[:80], "pid": os.getpid(),
                "session": record.get("parent_session_id") or record.get("session_key") or None}
        if record.get("dispatched_at"):
            unit["elapsed_s"] = round(now - float(record["dispatched_at"]), 1)
        units.append(unit)
    return units


def process_units(runner, now: float) -> list[dict]:
    """Background processes whose completion will start a new turn: a live notify watcher
    (``_run_process_watcher``) is still waiting on each."""
    from tools.process_registry import process_registry
    units = []
    for proc_id in sorted(getattr(runner, "_live_process_watchers", ()) or ()):
        session = process_registry.get(proc_id)
        unit = {"kind": "process", "process_id": proc_id}
        if session is not None:
            unit.update(pid=session.pid, command=str(session.command or "")[:80],
                        session=session.session_key or None, exited=bool(session.exited))
            if session.started_at:
                unit["elapsed_s"] = round(now - session.started_at, 1)
        units.append(unit)
    return units


def queued_notice_units(runner) -> list[dict]:
    """Completion notices queued for delivery, or dequeued by the watcher and not yet admitted
    (``_async_notices_in_delivery``): their turn has not started, but it will. One the watcher found
    no transport for here (``_no_transport``: a disabled or unavailable platform) is not counted: it
    would hold the restart forever, and its ledger row replays it after the restart."""
    from tools.process_registry import process_registry
    queue = process_registry.completion_queue
    with queue.mutex:
        events = list(queue.queue)
    events += list(getattr(runner, "_async_notices_in_delivery", ()) or ())
    return [{"kind": "notice", "type": "async_delegation",
             "id": evt.get("delegation_id") or evt.get("session_id"),
             "session": evt.get("parent_session_id") or evt.get("session_key") or None}
            # Only delegation completions: process completion events are re-queued by the shared
            # watcher until their own per-process watcher (counted above) consumes them.
            for evt in events if isinstance(evt, dict) and evt.get("type") == "async_delegation"
            and not evt.get("_no_transport")]


def background_units(runner) -> list[dict]:
    now = time.time()
    return delegation_units(now) + process_units(runner, now) + queued_notice_units(runner)


def _log_view(units: list[dict]) -> str:
    """Units for INFO/WARNING logs: kind, id and session only; goals and commands are user content."""
    keys = ("session", "delegation_id", "process_id", "id", "job_id", "pid")
    return "; ".join(f"{u.get('kind')}(" + ", ".join(f"{k}={u[k]}" for k in keys if u.get(k)) + ")"
                     for u in units if isinstance(u, dict)) or "nothing"


def force_stop_budgets(runner) -> None:
    """A forced restart does not drain: ``stop()`` interrupts turns, delegations and processes."""
    runner._restart_drain_timeout = 0.0
    runner._cron_drain_timeout = 0.0


def adopt_waiting_restart(runner) -> bool:
    """Make a restart that is still waiting exit for the service (exit 75) with no detached helper.

    An ordinary update pause that finds a ``/restart`` already waiting (it captured a detached
    helper) takes its plan over, so the gateway does not launch that helper mid-update and the
    updater hears ``pausing`` instead of stopping a gateway that is still doing work. False when no
    restart is waiting or its stop has begun (``_run_restart`` closes admission in the tick it reads
    the plan)."""
    if getattr(runner, "_restart_task_started", False) is not True:
        return False
    if getattr(runner, "_stop_task", None) is not None or getattr(runner, "_draining", False):
        return False
    runner._restart_detached, runner._restart_via_service = False, True
    return True


def request_service_restart(runner) -> bool:
    """An ordinary update pause: restart when idle and exit for the service, adopting a restart
    that is already waiting. False only when neither was possible (the stop has begun)."""
    return bool(runner.request_restart(detached=False, via_service=True)) or adopt_waiting_restart(runner)


def force_restart(runner, *, detached: bool = False, via_service: bool = True) -> bool:
    """Start a restart (or escalate a waiting one) that stops live work now. True when this call
    started or escalated it; False when ``stop()`` had already begun or the gateway is client-owned."""
    if getattr(runner, "_client_owned", False) or getattr(runner, "_stop_task", None) is not None:
        return False
    if getattr(runner, "_restart_force_event", None) is None:
        runner._restart_force_event = asyncio.Event()
    runner._restart_force_event.set()
    force_stop_budgets(runner)
    if not runner.request_restart(detached=detached, via_service=via_service):
        # A restart is already waiting: the force's relaunch plan replaces the one it captured
        # (an update pause must not launch a /restart's detached helper mid-update).
        runner._restart_detached, runner._restart_via_service = detached, via_service
    return True
