"""Bot-token lock ownership for the opt-in zero-cut Telegram handover."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from gateway.config import is_zero_cut_handover_enabled

# The loop holds tasks weakly: a teardown detached by the gateway's disconnect budget must stay
# rooted until it finishes, or GC can drop it before updater.stop() and the lock release run.
_DETACHED_TEARDOWNS: set[asyncio.Future] = set()


def _lock_owner_nonce(adapter) -> Optional[str]:
    """The per-acquire nonce on the adapter's token-lock record; None if missing or unreadable."""
    scope = getattr(adapter, "_platform_lock_scope", None)
    identity = getattr(adapter, "_platform_lock_identity", None)
    if not scope or not identity:
        return None
    from gateway.status import _get_scope_lock_path, _read_json_file
    record = _read_json_file(_get_scope_lock_path(scope, identity)) or {}
    return (record.get("metadata") or {}).get("nonce")


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
