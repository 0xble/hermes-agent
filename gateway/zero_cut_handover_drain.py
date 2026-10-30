"""Active-side zero-cut handover orchestration."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from gateway.config import is_zero_cut_handover_enabled

logger = logging.getLogger(__name__)


def _status(runner) -> dict[str, Any]:
    state = getattr(runner, "_zero_cut_handover_state", None)
    if state is None:
        return {
            "accepted": False, "updates_settled": False,
            "token_lock_released": False, "active_turns": runner._running_agent_count(),
            "idle": False, "failed": None,
        }
    active = runner._running_agent_count()
    queued = _queued_followups(runner)
    state["active_turns"] = active
    state["idle"] = bool(state["token_lock_released"] and not state.get("failed") and active == 0 and not queued)
    return dict(state)


def _queued_followups(runner) -> int:
    total = 0
    for values in (getattr(runner, "_pending_messages", {}), getattr(runner, "_queued_events", {})):
        if isinstance(values, dict):
            total += len(values)
            total += sum(len(value) for value in values.values() if isinstance(value, (list, tuple)))
    # The adapter owns the per-chat session guard and the follow-up merged in mid-turn; the runner's
    # turn count drops before the adapter finishes delivering the final and starting that follow-up.
    # Exiting in that gap flushed the follow-up to disk and left the final unconfirmed, so the
    # successor re-sent it as a "may be a duplicate" recovery (found in a real drill).
    for adapter in _adapters(runner):
        for name in ("_active_sessions", "_pending_messages"):
            values = getattr(adapter, name, None)
            if isinstance(values, dict):
                total += len(values)
    return total


def _adapters(runner):
    """Every served transport, deduplicating shared primary/profile registrations."""
    mappings = [getattr(runner, "adapters", {}) or {}]
    mappings.extend((getattr(runner, "_profile_adapters", {}) or {}).values())
    seen = set()
    for mapping in mappings:
        for adapter in mapping.values():
            if id(adapter) not in seen:
                seen.add(id(adapter))
                yield adapter


def _serves_telegram(runner) -> bool:
    return any(getattr(getattr(adapter, "platform", None), "value", None) == "telegram"
               for adapter in _adapters(runner))


async def _handover(runner) -> None:
    try:
        runner._draining = True
        state = runner._zero_cut_handover_state
        state["updates_settled"] = True
        state["token_lock_released"] = True
        await _wait_for_idle(runner)
    except asyncio.CancelledError:
        # The control client may already have promoted B. Do not publish failure (which
        # tells it to boot B out) after this commit point.
        runner._zero_cut_finish_task = asyncio.create_task(_wait_for_idle(runner))
        raise
    except Exception:
        logger.exception("Zero-cut handover failed")
        runner._zero_cut_finish_task = asyncio.create_task(_wait_for_idle(runner))


async def _wait_for_idle(runner) -> None:
    while True:
        status = _status(runner)
        if status["idle"]:
            # Own task with a strong ref outside _background_tasks: _stop_impl cancels that set, and
            # cancelling the task that awaits _stop_task would skip _shutdown_event.set() (#12875).
            runner._zero_cut_stop_task = asyncio.create_task(runner.stop(), name="zero-cut-handover-stop")
            return
        await asyncio.sleep(0.05)


def handover_runs_followups(runner) -> bool:
    """True while a released handover owner finishes its chats: unlike a restart drain, the process is
    not stopping, so a follow-up that landed mid-turn runs here instead of spooling to disk for a
    successor that only replays the spool at a later restart (found in a real drill)."""
    state = getattr(runner, "_zero_cut_handover_state", None) or {}
    stopping = getattr(runner, "_zero_cut_stop_task", None) is not None  # the real stop spools as today
    return bool(state.get("token_lock_released") and not state.get("failed") and not stopping)


def request_handover(runner, main_loop: asyncio.AbstractEventLoop) -> dict[str, Any]:
    """Accept once from the control-socket worker and marshal drain work to the loop."""
    if not is_zero_cut_handover_enabled(getattr(runner, "config", None)):
        return {"accepted": False, "refused": "disabled"}
    from gateway.zero_cut_standby import is_standby
    if is_standby():
        return {"accepted": False, "refused": "standby"}
    # Two Telegram pollers on one bot token conflict, and nothing transfers polling to the standby.
    if _serves_telegram(runner):
        return {"accepted": False, "refused": "telegram"}
    lock = getattr(runner, "_zero_cut_handover_lock", None)
    if lock is None:
        lock = runner._zero_cut_handover_lock = threading.Lock()
    with lock:
        state = getattr(runner, "_zero_cut_handover_state", None)
        if state is not None:
            return _status(runner)
        if getattr(runner, "_external_drain_active", False) or getattr(runner, "_draining", False):
            return {"accepted": False, "refused": "draining"}
        runner._zero_cut_handover_state = {
            "accepted": True, "updates_settled": False,
            "token_lock_released": False, "active_turns": runner._running_agent_count(),
            "idle": False, "failed": None,
        }
    runner._external_drain_active = True
    from cron.scheduler_review import stop_reviews
    stop_reviews()
    main_loop.call_soon_threadsafe(
        lambda: runner._retain_background_task(asyncio.create_task(_handover(runner), name="zero-cut-handover")))
    return _status(runner)


def status(runner) -> dict[str, Any]:
    return _status(runner)
