"""Reactive flood protection for drafts, controls and best-effort deletion.

This shares the adapter's known penalty and send lock. It is not a proactive
rate limiter or a durable retry queue. Failed deletions retain their cache owner.
"""

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


class FloodRefusal(Exception):
    def __init__(self, wait: float):
        self.retry_after = float(wait)
        super().__init__(f"flood_control:{self.retry_after}")


async def call_with_flood_guard(adapter: Any, chat_id: Any, call: Callable[[], Awaitable[T]]) -> T:
    # Import at call time: the adapter owns the existing RetryAfter classifier.
    from plugins.platforms.telegram.adapter import _telegram_retry_after

    async with adapter._chat_send_lock(chat_id):
        wait = adapter._send_flood_cooldown_remaining(chat_id)
        if wait is not None:
            raise FloodRefusal(wait)
        try:
            return await call()
        except Exception as exc:
            wait = _telegram_retry_after(exc)
            if wait is None:
                raise
            adapter._record_send_flood_cooldown(chat_id, wait)
            raise FloodRefusal(wait) from exc
