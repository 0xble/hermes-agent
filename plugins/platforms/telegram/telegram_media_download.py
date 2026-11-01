"""Retry read-only media fetches without changing outbound-message retry semantics."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)
_FETCH_DELAYS = (2.0, 5.0)


def _timeouts(attempt: int) -> dict[str, float]:
    from utils import env_float

    # Scale the same authored budgets used by _build_ptb_requests, not fixed replacements.
    return {name: env_float(f"HERMES_TELEGRAM_HTTP_{name.upper()}", default) * attempt
            for name, default in (("read_timeout", 20.0), ("connect_timeout", 10.0),
                                  ("write_timeout", 20.0), ("pool_timeout", 8.0))}


async def _fresh_request_fetch(source: Any, request_factory) -> tuple[Any, bytes]:
    """Escape a stale/saturated shared pool, keeping the adapter's proxy/TLS/IP policy.

    Do not reinitialize the live bot or borrow a user's Telegram account. Only getFile
    and the byte download are replayed; neither operation has ambiguous send effects.
    """
    from telegram import Bot

    original = source.get_bot()
    request, updates_request = await request_factory()
    async with request, updates_request:
        bot = Bot(token=original.token, request=request, get_updates_request=updates_request,
                  base_url=lambda _: original.base_url,
                  base_file_url=lambda _: original.base_file_url,
                  local_mode=original.local_mode)
        file_obj = await bot.get_file(source.file_id, **_timeouts(3))
        return file_obj, bytes(await file_obj.download_as_bytearray(**_timeouts(3)))


async def fetch_telegram_file(source: Any, what: str, *, request_factory) -> tuple[Any, bytes]:
    """Refetch metadata AND bytes after transient failures; preserve permanent errors/cancellation.

    Three attempts use increasing timeouts and cooperative backoff. A final isolated
    request-pool fallback runs only after those attempts fail. Size/auth/privacy checks
    remain with the caller, before any of these attempts.
    """
    from telegram.error import BadRequest, NetworkError

    for attempt in range(1, 4):
        try:
            file_obj = await source.get_file(**_timeouts(attempt))
            data = bytes(await file_obj.download_as_bytearray(**_timeouts(attempt)))
            if attempt > 1:
                logger.info("[Telegram] %s: media fetch recovered on attempt %d/3", what, attempt)
            return file_obj, data
        except BadRequest:  # PTB makes this permanent error a NetworkError subclass.
            raise
        except NetworkError as exc:
            # Error strings may contain a token-bearing URL; log only the type.
            logger.warning("[Telegram] %s: media fetch attempt %d/3 failed (%s)",
                           what, attempt, type(exc).__name__)
            if attempt < 3:
                await asyncio.sleep(_FETCH_DELAYS[attempt - 1])
    logger.warning("[Telegram] %s: trying isolated media request-pool fallback", what)
    result = await _fresh_request_fetch(source, request_factory)
    logger.info("[Telegram] %s: media fetch recovered via isolated request pool", what)
    return result
