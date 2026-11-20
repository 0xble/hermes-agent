"""Best-effort acknowledgements for inbound messages received while the gateway was offline."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

_LAST_START_KIND = "run"
_MAX_WINDOW_SECONDS = 24 * 60 * 60
_MAX_ACKS_PER_PLATFORM = 20
_DEFAULT_TEXT = "I was offline when you sent this and missed it. Ask again here if you still need it."


def _test_mode() -> bool:
    return bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("PYTEST_VERSION")
        or os.environ.get("HERMES_TEST_ISOLATION")
        or os.environ.get("EVAL_MODE")
        or os.environ.get("HERMES_EVAL")
    )


async def _call(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _state(db: Any, kind: str, *parts: str, default: Any = None) -> Any:
    getter = getattr(db, "get_missed_message_ack_state", None)
    if not callable(getter):
        return default
    value = await _call(getter(kind, *parts))
    return default if value is None else value


async def _set_state(db: Any, kind: str, value: Any, *parts: str) -> None:
    setter = getattr(db, "set_missed_message_ack_state", None)
    if callable(setter):
        await _call(setter(kind, value, *parts))


async def _claim(db: Any, platform: str, conversation: str, ts: str) -> bool:
    claim = getattr(db, "claim_missed_message_ack", None)
    if not callable(claim):
        return False
    return bool(await _call(claim(platform, conversation, ts)))


async def _mark_sent(db: Any, platform: str, conversation: str, ts: str) -> None:
    mark = getattr(db, "mark_missed_message_ack_sent", None)
    if callable(mark):
        await _call(mark(platform, conversation, ts))


async def _release(db: Any, platform: str, conversation: str, ts: str) -> None:
    release = getattr(db, "release_missed_message_ack", None)
    if callable(release):
        await _call(release(platform, conversation, ts))


def _config(runner) -> tuple[bool, str]:
    config = getattr(runner, "config", None)
    setting = getattr(config, "missed_message_ack", None)
    if isinstance(setting, dict):
        enabled = bool(setting.get("enabled", True))
        text = str(setting.get("text") or _DEFAULT_TEXT)
    else:
        enabled = bool(getattr(config, "missed_message_ack_enabled", True))
        text = str(getattr(config, "missed_message_ack_text", _DEFAULT_TEXT) or _DEFAULT_TEXT)
    # Tests and evals must never produce outbound messages, even when a test constructs a config directly.
    return enabled and not _test_mode(), text


def _key(item: dict[str, Any]) -> tuple[str, str, str]:
    return str(item.get("platform", "")), str(item.get("conversation", "")), str(item.get("ts", ""))


async def run_missed_message_ack(runner, *, now: float | None = None) -> None:
    lock = getattr(runner, "_missed_message_ack_lock", None)
    if lock is None:
        lock = runner._missed_message_ack_lock = asyncio.Lock()
    async with lock:
        await _run_missed_message_ack(runner, now=now)


async def _run_missed_message_ack(runner, *, now: float | None = None) -> None:
    """Fetch and acknowledge missed messages without entering the agent turn path."""
    enabled, text = _config(runner)
    if not enabled:
        return
    adapters = getattr(runner, "adapters", {}) or {}
    db = getattr(runner, "_session_db", None)
    if db is None:
        return
    now = time.time() if now is None else now
    previous = await _state(db, _LAST_START_KIND, default=None)
    # A first-ever run has no defensible offline interval. The boundary is written after the scan
    # is assembled so a history failure does not erase the previous window.
    since = float(previous) if isinstance(previous, (int, float)) else now
    since = max(since, now - _MAX_WINDOW_SECONDS)

    by_platform: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for platform, adapter in list(adapters.items()):
        fetch = getattr(adapter, "fetch_missed_inbound", None)
        if not callable(fetch):
            continue
        name = str(getattr(platform, "value", platform))
        try:
            records = await fetch(since, now)
            for item in records or []:
                if not isinstance(item, dict):
                    continue
                record = {**item, "platform": str(item.get("platform") or name)}
                try:
                    message_ts = float(record["ts"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not since <= message_ts <= now:
                    continue
                if record.get("conversation") and record.get("ts"):
                    by_platform[record["platform"]].append(record)
        except Exception:
            logger.warning("Missed-message scan failed for %s", name, exc_info=True)

    await _set_state(db, _LAST_START_KIND, now)
    for platform, records in by_platform.items():
        records.sort(key=lambda item: (str(item.get("conversation")), float(item.get("ts", 0))))
        individual = records[:_MAX_ACKS_PER_PLATFORM]
        overflow = records[_MAX_ACKS_PER_PLATFORM:]
        for item in individual:
            await _ack_one(db, runner, item, text)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in overflow:
            grouped[str(item["conversation"])].append(item)
        for conversation, items in grouped.items():
            await _ack_summary(db, runner, platform, conversation, items)


async def _ack_summary(db: Any, runner: Any, platform: str, conversation: str,
                       items: list[dict[str, Any]]) -> None:
    """Claim the deterministic summary and its covered messages before posting it."""
    first_ts = str(items[0]["ts"])
    last_ts = str(items[-1]["ts"])
    summary_ts = f"summary:{first_ts}:{last_ts}:{len(items)}"
    if not await _claim(db, platform, conversation, summary_ts):
        return
    claimed: list[tuple[str, str, str]] = []
    for item in items:
        key = _key(item)
        if await _claim(db, *key):
            claimed.append(key)
    if not claimed:
        await _release(db, platform, conversation, summary_ts)
        return
    summary = f"I was offline and missed {len(items)} messages here. Ask again here if you still need it."
    try:
        await _post_ack(
            runner,
            {**items[0], "ts": summary_ts, "thread_ts": items[0].get("thread_ts") or first_ts},
            summary,
        )
    except Exception:
        await _release(db, platform, conversation, summary_ts)
        for key in claimed:
            await _release(db, *key)
        logger.warning("Missed-message summary failed for %s:%s", platform, conversation, exc_info=True)
        return
    await _mark_sent(db, platform, conversation, summary_ts)
    for key in claimed:
        await _mark_sent(db, *key)


async def _ack_one(db: Any, runner: Any, item: dict[str, Any], text: str) -> None:
    platform, conversation, ts = _key(item)
    if not await _claim(db, platform, conversation, ts):
        return
    try:
        await _post_ack(runner, item, text)
    except Exception:
        await _release(db, platform, conversation, ts)
        logger.warning(
            "Missed-message acknowledgement failed for %s:%s:%s", platform, conversation, ts, exc_info=True,
        )
        return
    await _mark_sent(db, platform, conversation, ts)


async def _post_ack(runner, item: dict[str, Any], text: str) -> None:
    adapters = getattr(runner, "adapters", {}) or {}
    adapter = adapters.get(item.get("platform"))
    if adapter is None:
        adapter = next((a for p, a in adapters.items() if getattr(p, "value", p) == item.get("platform")), None)
    if adapter is None:
        raise RuntimeError("adapter unavailable")
    result = await adapter.send(
        str(item["conversation"]), text,
        reply_to=str(item.get("thread_ts") or item["ts"]),
        metadata={
            "thread_id": str(item.get("thread_ts") or item["ts"]),
            **({"scope_id": str(item["team_id"])} if item.get("team_id") else {}),
        },
    )
    if getattr(result, "success", True) is False:
        raise RuntimeError(getattr(result, "error", "send failed"))


async def start_missed_message_ack(runner) -> None:
    """Schedule the scan after adapters connect. Startup must not await it."""
    try:
        runner._retain_background_task(asyncio.create_task(
            run_missed_message_ack(runner), name="missed-message-ack"))
    except Exception:
        logger.warning("Could not start missed-message scan", exc_info=True)
