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
            await _await_with_thread_deadline(
                updater.stop(), timeout=_UPDATER_STOP_TIMEOUT,
                label="telegram handover updater.stop()")
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        await asyncio.wait_for(_wait_for_dispatch_settle(adapter, app), timeout=remaining)
    except asyncio.TimeoutError:
        return {"settled": False, "reason": "telegram updates did not settle before updater stop deadline"}
    except Exception as exc:
        logger.warning("Telegram handover polling stop failed: %s", exc, exc_info=True)
        return {"settled": False, "reason": type(exc).__name__}
    return {"settled": True}


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


async def run_disconnect(adapter, disconnect: Callable[..., Awaitable[None]]) -> None:
    """Keep the token lock through updater stop, even if the gateway detaches teardown.

    The gateway's disconnect budget may stop waiting while the shielded teardown keeps running. A
    successor adapter in this process can re-acquire the same lock file meanwhile (same-PID
    self-reacquire), so a late release only unlinks the record this adapter acquired, identified
    by the nonce it wrote at acquire time.
    """
    runner_config = getattr(getattr(adapter, "gateway_runner", None), "config", None)
    if not is_zero_cut_handover_enabled(runner_config):
        await disconnect(zero_cut_handover=None)
        return
    own = getattr(adapter, "_platform_lock_nonce", None)  # captured now: a later reacquire must not count

    def release() -> None:
        current = _lock_owner_nonce(adapter)
        if own is None or current == own:
            adapter._release_platform_lock()
        elif current is not None and getattr(adapter, "_platform_lock_nonce", None) == own:
            adapter._platform_lock_identity = None  # another adapter re-acquired the record
        # Unreadable record, or this adapter re-acquired: leave the identity for its later release.

    def release_after(task: asyncio.Future) -> None:
        _DETACHED_TEARDOWNS.discard(task)
        if not task.cancelled():
            task.exception()
        release()

    task = asyncio.ensure_future(disconnect(zero_cut_handover=release))
    try:
        await asyncio.shield(task)
    finally:
        if task.done():
            release()
        else:
            _DETACHED_TEARDOWNS.add(task)
            task.add_done_callback(release_after)
