"""Planned gateway restart handover entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextvars import Context
from typing import Optional

logger = logging.getLogger("gateway.run")


def request_restart(self, *, detached: bool = False, via_service: bool = False,
                    reason: Optional[str] = None) -> bool:
    """Request a planned handover when opt-in zero-cut mode is enabled."""
    if getattr(self, "_client_owned", False) or self._restart_task_started:
        return False
    from gateway.config import is_zero_cut_handover_enabled
    if is_zero_cut_handover_enabled(getattr(self, "config", None)):
        self._restart_task_started = True

        async def hand_over():
            try:
                from hermes_cli.gateway_handover import planned_handover
                if await asyncio.to_thread(planned_handover) is not True:
                    logger.error("Planned restart handover refused, existing work left intact")
            except Exception:
                logger.exception("Planned restart handover failed, no destructive fallback")
            finally:
                self._restart_task_started = False

        # Handover keeps the original owner alive for its in-flight work; it is not a shutdown drain.
        self._restart_task = Context().run(lambda: asyncio.create_task(hand_over()))
        return True
    self._restart_reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
    self._restart_requested = True
    self._restart_detached = detached
    self._restart_via_service = via_service
    self._restart_task_started = True
    # Refuse new turns only once the active-work wait completes; the requesting
    # turn and any follow-up admitted before that boundary keep being served.
    from gateway.drain_control import open_restart_wait
    open_restart_wait(self)  # clients may still answer a turn parked on approval/clarify (#139041)
    # The restart's after-turn wait is a drain window too: pollers of GET /v1/runs/{id} must see
    # the boundary from the moment new turns are refused, not only once stop() begins (#115133).
    self._mark_api_runs_shutdown_requested()

    async def _run_restart() -> None:
        await self._await_active_work_before_restart()
        # Admission closes only after the bounded wait.  A forced update pause
        # must never launch a detached helper captured by an earlier /restart.
        self._draining = True
        restart_detached = bool(getattr(self, "_restart_detached", detached))
        restart_via_service = bool(getattr(self, "_restart_via_service", via_service))
        if getattr(self, "_restart_force_event", None) is not None and self._restart_force_event.is_set():
            restart_detached = False
            restart_via_service = True
        # Detached helper only AFTER the after-turn wait, or its drain_timeout+5 deadline fires mid-turn.
        if restart_detached:
            from gateway.run_shutdown import _log_suppressed
            with _log_suppressed(logging.ERROR, "Failed to launch detached gateway restart helper: %s"):
                await self._launch_detached_restart_command()
        await asyncio.sleep(0.05)
        await self.stop(restart=True, detached_restart=restart_detached, service_restart=restart_via_service)

    # NOT in _background_tasks: _stop_impl cancels those, which would cancel _run_restart while it
    # awaits _stop_task and skip _shutdown_event.set() / _exit_code = 75 (#12875). We still hold a strong reference in
    # self._restart_task: a bare asyncio.create_task() keeps only a weak reference, so the event loop
    # may garbage-collect a still-pending task mid-flight. The cancel loop in _stop_impl explicitly
    # skips _restart_task for the same reason it skips _stop_task.
    # Empty Context: /restart is handled inside the requester's profile scope, and a copied context
    # would run the HOST restart as that profile (watcher HERMES_HOME, stop()'s flushes).
    self._restart_task = Context().run(lambda: asyncio.create_task(_run_restart()))
    return True
