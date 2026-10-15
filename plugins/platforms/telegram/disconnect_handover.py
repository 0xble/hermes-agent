"""Bot-token lock ownership for the opt-in zero-cut Telegram handover."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from gateway.config import is_zero_cut_handover_enabled


def _lock_stamp(adapter) -> Optional[str]:
    """The on-disk stamp of the adapter's token lock; every acquire rewrites it."""
    scope = getattr(adapter, "_platform_lock_scope", None)
    identity = getattr(adapter, "_platform_lock_identity", None)
    if not scope or not identity:
        return None
    from gateway.status import _get_scope_lock_path, _read_json_file
    return (_read_json_file(_get_scope_lock_path(scope, identity)) or {}).get("updated_at")


async def run_disconnect(adapter, disconnect: Callable[..., Awaitable[None]]) -> None:
    """Keep the token lock through updater stop, even if the gateway detaches teardown.

    The gateway's disconnect budget may stop waiting while the shielded teardown keeps running. A
    successor adapter in this process can re-acquire the same lock file meanwhile (same-PID
    self-reacquire), so a late release only unlinks the record this adapter acquired.
    """
    runner_config = getattr(getattr(adapter, "gateway_runner", None), "config", None)
    if not is_zero_cut_handover_enabled(runner_config):
        await disconnect(zero_cut_handover=None)
        return
    stamp = _lock_stamp(adapter)

    def release() -> None:
        if _lock_stamp(adapter) == stamp:
            adapter._release_platform_lock()
        else:
            adapter._platform_lock_identity = None  # a successor owns the record now

    def release_after(task: asyncio.Future) -> None:
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
            task.add_done_callback(release_after)
