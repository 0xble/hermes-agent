"""Carry root fallback through nested media/control sends without mutating caller metadata."""

import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _SendScope:
    task: asyncio.Task[Any] | None
    thread_id: int | str | None = None


_current: ContextVar[_SendScope | None] = ContextVar("telegram_thread_fallback", default=None)


def record_root_fallback(thread_id: int | str | None, occurred: bool = True) -> None:
    if thread_id is None or not occurred:
        return
    scope = _current.get()
    if scope is not None:
        if scope.thread_id is not None:
            return
        scope.thread_id = thread_id
    logger.warning("[Telegram] Thread %s unavailable; delivered to root", thread_id)


def reports_thread_fallback(method):
    @wraps(method)
    async def wrapped(*args, **kwargs):
        # Albums and file-to-text fallbacks share the enclosing send's outcome.
        # A concurrent child send owns a separate outcome even if context was copied.
        task = asyncio.current_task()
        scope = _current.get()
        if scope is None or scope.task is not task:
            scope = _SendScope(task)
        token = _current.set(scope)
        try:
            result = await method(*args, **kwargs)
            if scope.thread_id is not None:
                result.raw_response = {
                    **(result.raw_response or {}),
                    "thread_fallback": True,
                    "requested_thread_id": scope.thread_id,
                }
            return result
        finally:
            _current.reset(token)

    return wrapped
