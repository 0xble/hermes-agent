"""Bot-token lock ownership for the opt-in zero-cut Telegram handover."""

from __future__ import annotations

from typing import Awaitable, Callable

from gateway.config import is_zero_cut_handover_enabled


async def run_disconnect(adapter, disconnect: Callable[..., Awaitable[None]]) -> None:
    """Run Telegram teardown and, with zero-cut handover on, release the retained lock on any exit.

    The handover path keeps the bot-token lock until ``updater.stop()`` is done so a successor
    cannot poll beside the old updater. A try/finally in the calling coroutine, not a task
    callback, also covers inline callers such as the reconnect watcher, and holds no reference to
    the adapter once the call returns.
    """
    runner_config = getattr(getattr(adapter, "gateway_runner", None), "config", None)
    if not is_zero_cut_handover_enabled(runner_config):
        await disconnect(zero_cut_handover=False)
        return
    try:
        await disconnect(zero_cut_handover=True)
    finally:
        adapter._release_platform_lock()
