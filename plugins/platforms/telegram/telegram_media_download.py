"""Retry read-only media fetches without changing outbound-message retry semantics."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)
_FETCH_DELAYS = (2.0, 5.0)
# The serial update consumer must yield before the next 90s pending-update probe.
# httpx read timeouts limit inactivity, not the total duration of a slow transfer.
_FETCH_BUDGET = 60.0


def _timeouts(attempt: int) -> dict[str, float]:
    from utils import env_float

    # Scale the same authored budgets used by _build_ptb_requests, not fixed replacements.
    return {name: env_float(f"HERMES_TELEGRAM_HTTP_{name.upper()}", default) * attempt
            for name, default in (("read_timeout", 20.0), ("connect_timeout", 10.0),
                                  ("write_timeout", 20.0), ("pool_timeout", 8.0))}


async def _fetch_file_once(source: Any, attempt: int, local_root: str | None) -> tuple[Any, bytes]:
    """Fetch Telegram metadata and bytes once; retry policy stays with the caller."""
    file_obj = await source.get_file(**_timeouts(attempt))
    if local_root:
        from plugins.platforms.telegram.telegram_media_fetch import local_bot_api_path
        host_path = await asyncio.to_thread(
            local_bot_api_path, getattr(file_obj, "file_path", None), local_root)
        if host_path is not None:  # unmapped server path: fall through to PTB's own download
            return file_obj, await asyncio.to_thread(host_path.read_bytes)

    try:
        downloaded = await file_obj.download_as_bytearray(**_timeouts(attempt))
    except TypeError as exc:
        # Small PTB-compatible test doubles and older wrappers may expose
        # download_as_bytearray() without timeout keywords.
        if "unexpected keyword argument" not in str(exc):
            raise
        downloaded = await file_obj.download_as_bytearray()
    return file_obj, bytes(downloaded)


async def fetch_telegram_file(source: Any, what: str, local_root: str | None = None) -> tuple[Any, bytes]:
    """Retry metadata AND bytes at most three times within one wall-clock budget.

    The deadline includes both network stages and backoff, even with large authored
    timeouts. Use only the live bot's existing request pool; size/auth/privacy checks
    remain with the caller. Permanent errors and caller cancellation propagate.
    """
    from telegram.error import BadRequest, NetworkError, TimedOut

    try:
        async with asyncio.timeout(_FETCH_BUDGET):
            attempt = 1
            while True:
                try:
                    file_obj, data = await _fetch_file_once(source, attempt, local_root)
                    if attempt > 1:
                        logger.info("[Telegram] %s: media fetch recovered on attempt %d/3", what, attempt)
                    return file_obj, data
                except BadRequest:  # PTB makes this permanent error a NetworkError subclass.
                    raise
                except Exception as exc:
                    # Keep SDK-free test doubles and compatible PTB shims retryable by
                    # their stable exception name without broadening permanent failures.
                    if not isinstance(exc, NetworkError) and type(exc).__name__ != "TimedOut":
                        raise
                    # Error strings may contain a token-bearing URL; log only the type.
                    logger.warning("[Telegram] %s: media fetch attempt %d/3 failed (%s)",
                                   what, attempt, type(exc).__name__)
                    if attempt == 3:
                        raise
                    await asyncio.sleep(_FETCH_DELAYS[attempt - 1])
                    attempt += 1
    except TimeoutError as exc:
        # Reuse the existing caller-owned failure notice after the wall-clock deadline.
        raise TimedOut("Media fetch recovery budget exhausted") from exc
