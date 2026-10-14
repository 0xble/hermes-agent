"""Bot-token lock ownership for the opt-in zero-cut Telegram handover."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from gateway.config import is_zero_cut_handover_enabled


async def run_disconnect(adapter, disconnect: Callable[..., Awaitable[None]]) -> None:
    """Keep the token lock through updater stop, even if the gateway detaches teardown."""
    runner_config = getattr(getattr(adapter, "gateway_runner", None), "config", None)
    if not is_zero_cut_handover_enabled(runner_config):
        await disconnect(zero_cut_handover=False)
        return

    def release_after(task: asyncio.Future) -> None:
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        adapter._release_platform_lock()

    task = asyncio.ensure_future(disconnect(zero_cut_handover=True))
    try:
        await asyncio.shield(task)
    finally:
        if task.done():
            adapter._release_platform_lock()
        else:
            task.add_done_callback(release_after)
