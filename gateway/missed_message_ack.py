"""Best-effort acknowledgements for inbound messages received while the gateway was offline."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_CONNECTED_KIND = "connected"
_SHUTDOWN_KIND = "shutdown"
_SCAN_KIND = "scan"
_MAX_WINDOW_SECONDS = 24 * 60 * 60
_MAX_MESSAGES_PER_PLATFORM = 20
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
    return bool(await _call(claim(platform, conversation, ts))) if callable(claim) else False


async def _mark_sent(db: Any, platform: str, conversation: str, ts: str) -> None:
    mark = getattr(db, "mark_missed_message_ack_sent", None)
    if callable(mark):
        await _call(mark(platform, conversation, ts))


async def _release(db: Any, platform: str, conversation: str, ts: str) -> None:
    release = getattr(db, "release_missed_message_ack", None)
    if callable(release):
        await _call(release(platform, conversation, ts))


async def _window_start(db: Any, platform: str, now: float) -> float:
    values = [now - _MAX_WINDOW_SECONDS]
    for kind, parts in ((_CONNECTED_KIND, (platform,)), (_SHUTDOWN_KIND, ()), (_SCAN_KIND, (platform,))):
        value = await _state(db, kind, *parts)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return max(values)


async def record_missed_message_ack_shutdown(runner: Any, now: float | None = None) -> None:
    db = getattr(runner, "_session_db", None)
    if db is None:
        return
    try:
        await _set_state(db, _SHUTDOWN_KIND, time.time() if now is None else float(now))
    except Exception:
        logger.warning("Could not persist missed-message shutdown marker", exc_info=True)


def _enabled(runner: Any) -> bool:
    config = getattr(runner, "config", None)
    setting = getattr(config, "missed_inbound_recovery", None)
    enabled = bool(setting.get("enabled", True)) if isinstance(setting, dict) else bool(
        getattr(config, "missed_inbound_recovery_enabled", True))
    return enabled and not _test_mode()


def _adapter_for(runner: Any, platform: str) -> Any:
    for key, adapter in (getattr(runner, "adapters", {}) or {}).items():
        if str(getattr(key, "value", key)) == platform:
            return adapter
    return None


def _late_context(since: float, until: float, message_ts: str) -> str:
    start = datetime.fromtimestamp(since).astimezone().isoformat(timespec="minutes")
    sent = datetime.fromtimestamp(float(message_ts)).astimezone().isoformat(timespec="minutes")
    end = datetime.fromtimestamp(until).astimezone().isoformat(timespec="minutes")
    return (f"[Delivered late: sent {sent}; the gateway was offline from {start} to {end}. "
            "Acknowledge the delay, answer what still applies, and confirm before acting on anything time-sensitive.]\n\n")


def _key(item: dict[str, Any]) -> tuple[str, str, str]:
    return str(item.get("platform", "")), str(item.get("conversation", "")), str(item.get("ts", ""))


async def _post_ack(runner: Any, item: dict[str, Any], text: str) -> None:
    adapter = _adapter_for(runner, str(item.get("platform", "")))
    if adapter is None:
        raise RuntimeError("adapter unavailable")
    result = await _call(adapter.send(
        str(item["conversation"]), text,
        reply_to=str(item.get("thread_ts") or item["ts"]),
        metadata={"thread_id": str(item.get("thread_ts") or item["ts"]), **(
            {"scope_id": str(item["team_id"])} if item.get("team_id") else {})},
    ))
    if getattr(result, "success", True) is False:
        raise RuntimeError(getattr(result, "error", "send failed"))


async def _ack_one(db: Any, runner: Any, item: dict[str, Any], text: str) -> bool:
    platform, conversation, ts = _key(item)
    if not await _claim(db, platform, conversation, ts):
        return True
    try:
        adapter = _adapter_for(runner, platform)
        dispatch = getattr(adapter, "dispatch_missed_inbound", None) if adapter else None
        if callable(dispatch):
            await _call(dispatch(item))
        else:
            await _post_ack(runner, item, text)
    except Exception:
        await _release(db, platform, conversation, ts)
        logger.warning("Missed-message acknowledgement failed for %s:%s:%s", platform, conversation, ts, exc_info=True)
        return False
    await _mark_sent(db, platform, conversation, ts)
    return True


async def _post_summary(db: Any, runner: Any, platform: str, conversation: str,
                        items: list[dict[str, Any]]) -> bool:
    first_ts, last_ts = str(items[0]["ts"]), str(items[-1]["ts"])
    summary_ts = f"summary:{first_ts}:{last_ts}:{len(items)}"
    if not await _claim(db, platform, conversation, summary_ts):
        return True
    claimed: list[tuple[str, str, str]] = []
    for item in items:
        key = _key(item)
        if await _claim(db, *key):
            claimed.append(key)
    if not claimed:
        await _release(db, platform, conversation, summary_ts)
        return True
    try:
        await _post_ack(runner, {**items[0], "ts": summary_ts},
                        f"I was offline and missed {len(items)} messages here. Ask again here if you still need it.")
    except Exception:
        await _release(db, platform, conversation, summary_ts)
        for key in claimed:
            await _release(db, *key)
        logger.warning("Missed-message summary failed for %s:%s", platform, conversation, exc_info=True)
        return False
    await _mark_sent(db, platform, conversation, summary_ts)
    for key in claimed:
        await _mark_sent(db, *key)
    return True


_ack_summary = _post_summary


async def run_missed_message_ack(runner, *, now: float | None = None, platform: Any | None = None) -> None:
    lock = getattr(runner, "_missed_message_ack_lock", None)
    if lock is None:
        lock = runner._missed_message_ack_lock = asyncio.Lock()
    async with lock:
        try:
            await _run_missed_message_ack(runner, now=now, platform=platform)
        except Exception:
            logger.warning("Missed-message scan failed", exc_info=True)


async def _run_missed_message_ack(runner, *, now: float | None = None, platform: Any | None = None) -> None:
    """Fetch missed inbound records and feed them through normal adapter handlers."""
    if not _enabled(runner):
        return
    db = getattr(runner, "_session_db", None)
    if db is None:
        return
    now = time.time() if now is None else float(now)
    candidates = list((getattr(runner, "adapters", {}) or {}).items())
    platform_name = str(getattr(platform, "value", platform)) if platform is not None else None
    if platform_name:
        candidates = [(key, adapter) for key, adapter in candidates if str(getattr(key, "value", key)) == platform_name]
    by_platform: dict[str, list[dict[str, Any]]] = defaultdict(list)
    successful: set[str] = set()
    for key, adapter in candidates:
        fetch = getattr(adapter, "fetch_missed_inbound", None)
        if not callable(fetch):
            continue
        name = str(getattr(key, "value", key))
        since = await _window_start(db, name, now)
        try:
            records = await _call(fetch(since, now))
            successful.add(name)
            for raw in records or []:
                if not isinstance(raw, dict):
                    continue
                item = {**raw, "platform": str(raw.get("platform") or name)}
                try:
                    timestamp = float(item["ts"])
                except (KeyError, TypeError, ValueError):
                    continue
                if since <= timestamp <= now and item.get("conversation") and item.get("ts"):
                    item["late_context"] = _late_context(since, now, str(item["ts"]))
                    by_platform[item["platform"]].append(item)
        except Exception:
            logger.warning("Missed-message scan failed for %s", name, exc_info=True)
    for name, records in by_platform.items():
        records.sort(key=lambda item: (str(item.get("conversation")), float(item.get("ts", 0))))
        individual, overflow = records[:_MAX_MESSAGES_PER_PLATFORM], records[_MAX_MESSAGES_PER_PLATFORM:]
        ok = True
        for item in individual:
            ok = await _ack_one(db, runner, item, "") and ok
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in overflow:
            grouped[str(item["conversation"])].append(item)
        for conversation, items in grouped.items():
            ok = await _post_summary(db, runner, name, conversation, items) and ok
        if ok:
            await _set_state(db, _SCAN_KIND, now, name)
    for name in successful:
        if name not in by_platform:
            await _set_state(db, _SCAN_KIND, now, name)


def schedule_missed_message_ack(runner: Any, platform: Any) -> None:
    """Schedule one platform scan without making adapter publication wait for history."""
    try:
        runner._retain_background_task(asyncio.create_task(
            run_missed_message_ack(runner, platform=platform), name="missed-message-ack"))
    except Exception:
        logger.warning("Could not start missed-message scan", exc_info=True)


async def start_missed_message_ack(runner: Any, *, platform: Any | None = None) -> None:
    """Schedule recovery after startup or one platform reconnect without blocking either path."""
    schedule_missed_message_ack(runner, platform)
