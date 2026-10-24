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


def _telegram_adapters(runner):
    for adapter in _adapters(runner):
        platform = getattr(adapter, "platform", None)
        if getattr(platform, "value", platform) == "telegram":
            yield adapter


async def _handover(runner) -> None:
    from plugins.platforms.telegram.disconnect_handover import handover_stop_polling, release_handover_lock

    adapters = list(_telegram_adapters(runner))
    ownership = [(adapter, tuple(getattr(adapter, name, None) for name in (
        "_platform_lock_scope", "_platform_lock_identity", "_platform_lock_nonce"))) for adapter in adapters]
    try:
        for adapter in adapters:
            result = await handover_stop_polling(adapter)
            if not result.get("settled"):
                await asyncio.shield(_fail(runner, result.get("reason", "telegram updates did not settle"), ownership))
                return
        runner._draining = True
        state = runner._zero_cut_handover_state
        state["updates_settled"] = True
        for adapter in adapters:
            release_handover_lock(adapter)
        state["token_lock_released"] = True
        await _wait_for_idle(runner)
    except asyncio.CancelledError:
        if runner._zero_cut_handover_state["token_lock_released"]:
            # The control client may already have promoted B. Do not publish failure (which
            # tells it to boot B out), or restart A's poller, after this commit point.
            runner._zero_cut_finish_task = asyncio.create_task(_wait_for_idle(runner))
        else:
            _fail(runner, "handover cancelled", ownership)
        raise
    except Exception as exc:
        logger.exception("Zero-cut handover failed")
        if runner._zero_cut_handover_state["token_lock_released"]:
            runner._zero_cut_finish_task = asyncio.create_task(_wait_for_idle(runner))
        else:
            await asyncio.shield(_fail(runner, str(exc) or type(exc).__name__, ownership))


async def _wait_for_idle(runner) -> None:
    while True:
        status = _status(runner)
        if status["idle"]:
            # Own task with a strong ref outside _background_tasks: _stop_impl cancels that set, and
            # cancelling the task that awaits _stop_task would skip _shutdown_event.set() (#12875).
            runner._zero_cut_stop_task = asyncio.create_task(runner.stop(), name="zero-cut-handover-stop")
            return
        await asyncio.sleep(0.05)


def _fail(runner, reason: str, ownership) -> asyncio.Task:
    state = runner._zero_cut_handover_state
    existing = getattr(runner, "_zero_cut_recovery_task", None)
    if existing is not None:
        return existing
    state["failed"] = reason
    state["idle"] = False
    state["recovering"] = True
    logger.error("Zero-cut handover failed; restoring old transport ownership: %s", reason)
    # A failure is not a shutdown. Preserve a concurrent dashboard drain, and never
    # resurrect a runner whose real stop already owns teardown.
    if not getattr(runner, "_stop_task", None) and getattr(runner, "_running", False):
        from gateway.drain_control import drain_requested
        runner._draining = False
        runner._external_drain_active = drain_requested()
    runner._zero_cut_recovery_task = asyncio.create_task(_recover(runner, ownership), name="zero-cut-handover-recovery")
    return runner._zero_cut_recovery_task


async def _recover(runner, ownership) -> None:
    from plugins.platforms.telegram.disconnect_handover import resume_handover_polling
    state = runner._zero_cut_handover_state
    if getattr(runner, "_stop_task", None) or not getattr(runner, "_running", False):
        state["recovering"] = False
        return
    # Restore siblings independently: one broken credential must not strand every
    # served bot, including the ones stopped before that sibling failed.
    results = await asyncio.gather(*(resume_handover_polling(adapter, lock) for adapter, lock in ownership),
                                   return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            logger.error("Telegram handover recovery failed: %s", result, exc_info=result)
    # Keep received input admissible across late batches/held redispatch, even if a
    # dashboard drain arrived meanwhile. New input uses ordinary admission once these
    # existing task owners have handed their events to the runner.
    await asyncio.gather(*(_settle_recovery_input(adapter) for adapter, _ in ownership))
    state["recovering"] = False


async def _settle_recovery_input(adapter) -> None:
    """Follow the existing held-input ownership transfer, including a retryable rebuild."""
    while True:
        successor = getattr(adapter, "_held_inbound_successor", None)
        target = successor() if successor is not None else None
        if target is not None:
            adapter = target
            continue
        queue = getattr(getattr(adapter, "_app", None), "update_queue", None)
        if queue is not None and hasattr(queue, "join"):
            await queue.join()
        tasks = [task for name in ("_pending_text_batch_tasks", "_pending_photo_batch_tasks", "_media_group_tasks")
                 for task in (getattr(adapter, name, {}) or {}).values()]
        held = getattr(adapter, "_held_inbound_redispatch_task", None)
        if held is not None:
            tasks.append(held)
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
        successor = getattr(adapter, "_held_inbound_successor", None)
        if successor is not None and successor() is not None:
            continue
        if (getattr(adapter, "fatal_error_code", None) and getattr(adapter, "fatal_error_retryable", False)
                and getattr(adapter, "_held_inbound_events", None)):
            # The reconnect owner, not this failed handover, publishes the replacement.
            # Its existing weak successor link transfers both held and late arrivals.
            await asyncio.sleep(0.05)
            continue
        return


def handover_runs_followups(runner) -> bool:
    """True while a released handover owner finishes its chats: unlike a restart drain, the process is
    not stopping, so a follow-up that landed mid-turn runs here instead of spooling to disk for a
    successor that only replays the spool at a later restart (found in a real drill)."""
    state = getattr(runner, "_zero_cut_handover_state", None) or {}
    stopping = getattr(runner, "_zero_cut_stop_task", None) is not None  # the real stop spools as today
    return bool(state.get("token_lock_released") and not state.get("failed") and not stopping)


def handover_accepts_received(runner, source) -> bool:
    """Keep the old Telegram transport's received work admissible until its chats finish.

    The early external-drain flag gates cron when observed by its dispatch closure; the
    dashboard marker watcher can clear this shared flag independently. Polling settlement
    is not runner admission: a popped batch can still await identity recovery or a hook.
    Only a registered receiving Telegram adapter, never restored or relay input, owns
    this allowance. Recovery retains it for late received work, not for a real stop.
    """
    state = getattr(runner, "_zero_cut_handover_state", None) or {}
    if (not state.get("accepted") or (state.get("failed") and not state.get("recovering"))
            or getattr(runner, "_zero_cut_stop_task", None) is not None
            or getattr(runner, "_stop_task", None)):
        return False
    owner = runner._transport_owner(source)
    return bool(owner and any(owner[0] is adapter for adapter in _telegram_adapters(runner)))


def request_handover(runner, main_loop: asyncio.AbstractEventLoop) -> dict[str, Any]:
    """Accept once from the control-socket worker and marshal drain work to the loop."""
    if not is_zero_cut_handover_enabled(getattr(runner, "config", None)):
        return {"accepted": False, "refused": "disabled"}
    from gateway.zero_cut_standby import is_standby
    if is_standby():
        return {"accepted": False, "refused": "standby"}
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
