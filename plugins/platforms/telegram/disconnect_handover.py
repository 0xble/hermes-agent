"""Bot-token lock ownership for the opt-in zero-cut Telegram handover."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from gateway.config import is_zero_cut_handover_enabled

logger = logging.getLogger(__name__)

# The loop holds tasks weakly: a teardown detached by the gateway's disconnect budget must stay
# rooted until it finishes, or GC can drop it before updater.stop() and the lock release run.
_DETACHED_TEARDOWNS: set[asyncio.Future] = set()


async def handover_stop_polling(adapter) -> dict[str, object]:
    """Stop Telegram polling and settle PTB dispatch without tearing down the Bot."""
    # Reuse the polling lifecycle fence so watchdog/recovery cannot restart this old poller.
    # This is separate from the delayed-delivery drop guard: fetched batches must still dispatch.
    adapter._polling_teardown_started = True
    app = getattr(adapter, "_app", None)
    from plugins.platforms.telegram.adapter import _UPDATER_STOP_TIMEOUT, _await_with_thread_deadline

    deadline = asyncio.get_running_loop().time() + _UPDATER_STOP_TIMEOUT
    updater = getattr(app, "updater", None)
    try:
        if updater is not None and (not hasattr(updater, "running") or updater.running):
            # Retain the actual operation, not the deadline wrapper. A timed-out/cancelled
            # stop can still own PTB's lifecycle lock and must never race an in-place start.
            adapter._handover_polling_stop_task = asyncio.create_task(updater.stop())
            await _await_with_thread_deadline(
                adapter._handover_polling_stop_task, timeout=_UPDATER_STOP_TIMEOUT,
                label="telegram handover updater.stop()")
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        await asyncio.wait_for(_wait_for_dispatch_settle(adapter, app), timeout=remaining)
    except asyncio.TimeoutError:
        return {"settled": False, "reason": "telegram updates did not settle before updater stop deadline"}
    except Exception as exc:
        logger.warning("Telegram handover polling stop failed: %s", exc, exc_info=True)
        return {"settled": False, "reason": type(exc).__name__}
    return {"settled": True}


def _restore_handover_lock(adapter, ownership) -> bool:
    """Reclaim a partial release, never replacing a different live acquire (even same PID)."""
    scope, identity, nonce = ownership
    if not identity:
        return True  # no scoped credential (also supports adapters built without connect())
    from gateway.status import _get_scope_lock_path, _read_json_file, acquire_scoped_lock
    path = _get_scope_lock_path(scope, identity)
    record = _read_json_file(path)
    if record is not None:
        if (record.get("metadata") or {}).get("nonce") != nonce:
            return False
    elif path.exists():
        return False  # unreadable is not proof that ownership is available
    else:
        acquired, _ = acquire_scoped_lock(scope, identity, metadata={"platform": "telegram", "nonce": nonce})
        if not acquired:
            return False
    adapter._platform_lock_identity = identity
    return True


async def resume_handover_polling(adapter, ownership) -> None:
    """Undo only a settled, still-owned handover fence. Ambiguous PTB teardown uses its
    existing retryable-fatal/reconnect owner rather than starting a second lifecycle."""
    if not getattr(adapter, "_polling_teardown_started", False):
        return
    stop = getattr(adapter, "_handover_polling_stop_task", None)
    safe_stop = stop is None or (stop.done() and not stop.cancelled() and stop.exception() is None)
    if not safe_stop or not _restore_handover_lock(adapter, ownership):
        # Publish the real retirement owner before fatal notification can queue a
        # same-process reconnect. PTB running=False is not completion of its cleanup.
        _start_disconnect(adapter, adapter._disconnect)
        await adapter._go_fatal_network(
            "Telegram handover could not safely resume its poller; rebuilding via gateway recovery.",
            "[%s] %s", adapter.name,
            "handover rollback requires adapter recovery")
        return
    adapter._polling_teardown_started = False
    adapter._schedule_held_inbound_redispatch()
    app = getattr(adapter, "_app", None)
    updater = getattr(app, "updater", None)
    if updater is not None and not updater.running:
        try:
            await adapter._start_polling_resilient(
                drop_pending_updates=False, error_callback=adapter._polling_error_callback_ref)
        except Exception:
            # Any restoration failure must reach gateway recovery; keep its traceback.
            logger.warning("[%s] handover polling restoration failed", adapter.name, exc_info=True)
            await adapter._go_fatal_network(
                "Telegram handover polling restoration failed; rebuilding via gateway recovery.",
                "[%s] %s", adapter.name, "handover polling restoration failed")


async def _wait_for_dispatch_settle(adapter, app) -> None:
    # PTB dequeues before its concurrent processor starts. Its queue join spans that gap, unlike
    # empty()/processor counters, and updater.stop() above has already stopped new fetches.
    queue = getattr(app, "update_queue", None)
    if queue is not None and hasattr(queue, "join"):
        await queue.join()
    while True:
        if getattr(adapter, "_held_inbound_events", None):
            raise RuntimeError("telegram received input remains held")
        # Batch dictionaries are popped before awaiting handle_message (identity recovery), and
        # held redispatch pops its queue before dispatch too. Join their existing task owners.
        tasks = [task for name in (
            "_pending_text_batch_tasks", "_pending_photo_batch_tasks", "_media_group_tasks"
        ) for task in (getattr(adapter, name, {}) or {}).values() if not task.done()]
        tasks.extend(task for task in getattr(adapter, "_committed_flush_tasks", {}).values() if not task.done())
        redispatch = getattr(adapter, "_held_inbound_redispatch_task", None)
        if redispatch is not None and not redispatch.done():
            tasks.append(redispatch)
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
            continue
        queue = getattr(app, "update_queue", None)
        queue_empty = queue is None or bool(getattr(queue, "empty", lambda: True)())
        processor = getattr(app, "update_processor", None)
        current = getattr(processor, "current_concurrent_updates", None)
        if current is None:
            semaphore = getattr(processor, "_semaphore", None)
            maximum = getattr(processor, "max_concurrent_updates", None)
            available = getattr(semaphore, "current_value", None)
            current = None if maximum is None or available is None else maximum - available
        processor_idle = current in (None, 0)
        inflight = getattr(adapter, "_inflight_update_ids", {})
        # Text/photo/media-group batches buffer already-fetched updates for a short delay before
        # dispatch; releasing the lock first would strand them behind the drain gate.
        buffered = any(getattr(adapter, name, None) for name in (
            "_pending_text_batches", "_pending_photo_batches", "_media_group_events"))
        if queue_empty and processor_idle and not inflight and not buffered:
            return
        await asyncio.sleep(0.01)


def _lock_owner_nonce(adapter) -> Optional[str]:
    """The per-acquire nonce on the adapter's token-lock record; None if missing or unreadable."""
    scope = getattr(adapter, "_platform_lock_scope", None)
    identity = getattr(adapter, "_platform_lock_identity", None)
    if not scope or not identity:
        return None
    from gateway.status import _get_scope_lock_path, _read_json_file
    record = _read_json_file(_get_scope_lock_path(scope, identity)) or {}
    return (record.get("metadata") or {}).get("nonce")


def release_handover_lock(adapter) -> None:
    """Release only the lock acquired by this adapter, preserving successor ownership."""
    own = getattr(adapter, "_platform_lock_nonce", None)
    current = _lock_owner_nonce(adapter)
    if own is None or current == own:
        adapter._release_platform_lock()
    elif current is not None and getattr(adapter, "_platform_lock_nonce", None) == own:
        adapter._platform_lock_identity = None


def _start_disconnect(adapter, disconnect: Callable[..., Awaitable[None]]) -> asyncio.Future:
    """One shielded credential-retirement owner, also joined by runner reconnects."""
    existing = getattr(adapter, "_platform_lock_retirement_task", None)
    if existing is not None:
        return existing
    own = getattr(adapter, "_platform_lock_nonce", None)

    def release() -> None:
        current = _lock_owner_nonce(adapter)
        if own is None or current == own:
            adapter._release_platform_lock()
        elif current is not None and getattr(adapter, "_platform_lock_nonce", None) == own:
            adapter._platform_lock_identity = None
        # An unreadable record is not proof that a successor no longer owns it.

    async def retire() -> None:
        stop = getattr(adapter, "_handover_polling_stop_task", None)
        if stop is not None:
            # A deadline/cancel only ended the handover's wait. PTB's final getUpdates
            # can still be live, even with running=False. Neither stop it twice nor
            # close its app/release its token until the actual operation finishes.
            await asyncio.gather(asyncio.shield(stop), return_exceptions=True)
        await disconnect(zero_cut_handover=release)

    def finished(task: asyncio.Future) -> None:
        _DETACHED_TEARDOWNS.discard(task)
        if not task.cancelled():
            task.exception()
        release()

    task = adapter._platform_lock_retirement_task = asyncio.create_task(retire())
    _DETACHED_TEARDOWNS.add(task)
    task.add_done_callback(finished)
    return task


async def run_disconnect(adapter, disconnect: Callable[..., Awaitable[None]]) -> None:
    """A gateway deadline detaches its wait, not the old token user's lifetime.

    Repeated retirement callers join the same operation. Keep the original nonce
    release check: unrelated same-PID reacquisition must never be unlinked late.
    """
    runner_config = getattr(getattr(adapter, "gateway_runner", None), "config", None)
    if not is_zero_cut_handover_enabled(runner_config):
        await disconnect(zero_cut_handover=None)
        return
    await asyncio.shield(_start_disconnect(adapter, disconnect))
