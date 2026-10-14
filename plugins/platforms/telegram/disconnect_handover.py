"""Cancellation-safe Telegram updater handover teardown."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger("plugins.platforms.telegram")


def arm_abort_release(adapter, task) -> None:
    """Release an opt-in retained lock if disconnect aborts before updater.stop()."""
    def release_on_abort(done_task) -> None:
        if done_task.cancelled():
            adapter._release_platform_lock()
            return
        try:
            failed = done_task.exception() is not None
        except asyncio.CancelledError:
            failed = True
        if failed:
            adapter._release_platform_lock()

    task.add_done_callback(release_on_abort)


async def stop_updater_for_handover(
    adapter, updater, await_step: Callable[[Awaitable, float, str], Awaitable[bool]], timeout: float,
) -> bool:
    """Stop PTB and release the retained token lock at completion or the bounded abort boundary."""
    task = asyncio.ensure_future(updater.stop())

    def release(done_task) -> None:
        try:
            done_task.exception()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Telegram updater stop failed after handover detach", exc_info=True)
        adapter._release_platform_lock()

    task.add_done_callback(release)
    try:
        stopped = await await_step(asyncio.shield(task), timeout, "updater.stop()")
    except asyncio.CancelledError:
        adapter._release_platform_lock()
        raise
    if not stopped:
        logger.warning("[%s] updater.stop() timed out; releasing the bot-token lock", adapter.name)
        adapter._release_platform_lock()
    return stopped
