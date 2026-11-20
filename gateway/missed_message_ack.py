"""Recover inbound messages that arrived while a gateway platform was offline."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from gateway.config import _test_mode

logger = logging.getLogger(__name__)
_CONNECTED_KIND = "connected"
_SHUTDOWN_KIND = "shutdown"
_SCAN_KIND = "scan"
_MAX_WINDOW_SECONDS = 24 * 60 * 60
_MAX_MESSAGES_PER_PLATFORM = 20


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
    """Use only persisted liveness, never a previous scan/run start."""
    values = []
    for kind, parts in ((_CONNECTED_KIND, (platform,)), (_SHUTDOWN_KIND, ())):
        value = await _state(db, kind, *parts)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return max(now - _MAX_WINDOW_SECONDS, *values) if values else now


async def record_missed_message_ack_connected(runner: Any, platform: Any, now: float | None = None) -> None:
    db = getattr(runner, "_session_db", None)
    name = str(getattr(platform, "value", platform))
    if db is None or not name:
        return
    value = time.time() if now is None else float(now)
    current = await _state(db, _CONNECTED_KIND, name, default=0.0)
    if not isinstance(current, (int, float)) or value > float(current):
        await _set_state(db, _CONNECTED_KIND, value, name)


async def record_missed_message_ack_activity(runner: Any, source: Any, timestamp: Any = None) -> None:
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))
    try:
        value = float(timestamp) if timestamp is not None else time.time()
    except (TypeError, ValueError):
        value = time.time()
    await record_missed_message_ack_connected(runner, platform, value)


def _schedule(coro: Any, runner: Any, name: str) -> None:
    try:
        task = asyncio.create_task(coro, name=name)
        retain = getattr(runner, "_retain_background_task", None)
        if callable(retain):
            retain(task)
    except Exception:
        logger.debug("Could not schedule missed-inbound state update", exc_info=True)


def schedule_missed_message_ack_activity(runner: Any, source: Any, timestamp: Any = None) -> None:
    _schedule(record_missed_message_ack_activity(runner, source, timestamp), runner,
              "missed-inbound-connected-watermark")


def schedule_missed_message_ack_connected(runner: Any, platform: Any, now: float | None = None) -> None:
    _schedule(record_missed_message_ack_connected(runner, platform, now), runner,
              "missed-inbound-platform-watermark")


async def record_missed_message_ack_shutdown(runner: Any, now: float | None = None) -> None:
    db = getattr(runner, "_session_db", None)
    if db is None:
        return
    value = time.time() if now is None else float(now)
    try:
        await _set_state(db, _SHUTDOWN_KIND, value)
        for platform in (getattr(runner, "adapters", {}) or {}):
            await record_missed_message_ack_connected(runner, platform, value)
    except Exception:
        logger.debug("Could not persist missed-inbound shutdown marker", exc_info=True)


def _enabled(runner: Any) -> bool:
    config = getattr(runner, "config", None)
    setting = getattr(config, "missed_inbound_recovery", None)
    enabled = bool(setting.get("enabled", True)) if isinstance(setting, dict) else bool(
        getattr(config, "missed_inbound_recovery_enabled", True))
    return enabled and not _test_mode()


def _key(item: dict[str, Any]) -> tuple[str, str, str]:
    return str(item.get("platform", "")), str(item.get("conversation", "")), str(item.get("ts", ""))


def _adapter_for(runner: Any, platform: str) -> Any:
    for key, adapter in (getattr(runner, "adapters", {}) or {}).items():
        if str(getattr(key, "value", key)) == platform:
            return adapter
    return None


def _late_note(since: float, until: float, message_ts: str) -> str:
    start = datetime.fromtimestamp(since).astimezone().isoformat(timespec="minutes")
    sent = datetime.fromtimestamp(float(message_ts)).astimezone().isoformat(timespec="minutes")
    end = datetime.fromtimestamp(until).astimezone().isoformat(timespec="minutes")
    return (f"[Delivered late: sent {sent}; the gateway was offline from {start} to {end}. "
            "Acknowledge the delay, answer what still applies, and confirm before acting on anything time-sensitive.]\n\n")


async def _dispatch_one(db: Any, runner: Any, item: dict[str, Any], since: float, until: float) -> bool:
    platform, conversation, ts = _key(item)
    if not await _claim(db, platform, conversation, ts):
        return True
    adapter = _adapter_for(runner, platform)
    try:
        dispatch = getattr(adapter, "dispatch_missed_inbound", None) if adapter else None
        if not callable(dispatch):
            raise RuntimeError(f"{platform} adapter does not support missed inbound recovery")
        await _call(dispatch({**item, "late_context": _late_note(since, until, ts)}))
    except Exception:
        await _release(db, platform, conversation, ts)
        logger.warning("Missed inbound dispatch failed for %s:%s:%s", platform, conversation, ts, exc_info=True)
        return False
    await _mark_sent(db, platform, conversation, ts)
    return True


async def _post_summary(db: Any, runner: Any, platform: str, conversation: str,
                        items: list[dict[str, Any]]) -> bool:
    first_ts, last_ts = str(items[0]["ts"]), str(items[-1]["ts"])
    summary_ts = f"summary:{first_ts}:{last_ts}:{len(items)}"
    if not await _claim(db, platform, conversation, summary_ts):
        return True
    claimed = [key for item in items if await _claim(db, *_key(item)) for key in [_key(item)]]
    if not claimed:
        await _release(db, platform, conversation, summary_ts)
        return True
    try:
        adapter = _adapter_for(runner, platform)
        result = await _call(adapter.send(
            conversation,
            f"I was offline and missed {len(items)} messages here. Ask again here if you still need it.",
            reply_to=str(items[0].get("thread_ts") or first_ts),
            metadata={"thread_id": str(items[0].get("thread_ts") or first_ts),
                      **({"scope_id": str(items[0]["team_id"])} if items[0].get("team_id") else {})},
        ))
        if getattr(result, "success", True) is False:
            raise RuntimeError(getattr(result, "error", "summary send failed"))
    except Exception:
        await _release(db, platform, conversation, summary_ts)
        for key in claimed:
            await _release(db, *key)
        logger.warning("Missed-inbound overflow summary failed for %s:%s", platform, conversation, exc_info=True)
        return False
    await _mark_sent(db, platform, conversation, summary_ts)
    for key in claimed:
        await _mark_sent(db, *key)
    return True


async def _run_missed_message_ack(runner: Any, *, now: float | None = None,
                                  platform_only: str | None = None) -> None:
    if not _enabled(runner):
        return
    db = getattr(runner, "_session_db", None)
    if db is None:
        return
    now = time.time() if now is None else float(now)
    by_platform: dict[str, list[dict[str, Any]]] = defaultdict(list)
    successful: dict[str, bool] = {}
    for key, adapter in list((getattr(runner, "adapters", {}) or {}).items()):
        name = str(getattr(key, "value", key))
        if platform_only and name != platform_only:
            continue
        fetch = getattr(adapter, "fetch_missed_inbound", None)
        if not callable(fetch):
            continue
        since = await _window_start(db, name, now)
        try:
            records = await _call(fetch(since, now))
            successful[name] = True
            for raw in records or []:
                if not isinstance(raw, dict):
                    continue
                item = {**raw, "platform": str(raw.get("platform") or name)}
                try:
                    ts = float(item["ts"])
                except (KeyError, TypeError, ValueError):
                    continue
                if since <= ts <= now and item.get("conversation") and item.get("ts"):
                    by_platform[item["platform"]].append(item)
        except Exception:
            logger.warning("Missed-inbound scan failed for %s; stopping this scan", name, exc_info=True)
    for name, records in by_platform.items():
        records.sort(key=lambda item: (str(item.get("conversation")), float(item.get("ts", 0))))
        individual, overflow = records[:_MAX_MESSAGES_PER_PLATFORM], records[_MAX_MESSAGES_PER_PLATFORM:]
        since = await _window_start(db, name, now)
        ok = successful.get(name, True)
        for item in individual:
            ok = await _dispatch_one(db, runner, item, since, now) and ok
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in overflow:
            grouped[str(item["conversation"])].append(item)
        for conversation, items in grouped.items():
            ok = await _post_summary(db, runner, name, conversation, items) and ok
        if ok:
            await _set_state(db, _SCAN_KIND, now, name)
    for name, ok in successful.items():
        if ok and name not in by_platform:
            await _set_state(db, _SCAN_KIND, now, name)


async def run_missed_message_ack(runner: Any, *, now: float | None = None,
                                 platform_only: str | None = None) -> None:
    lock = getattr(runner, "_missed_message_ack_lock", None)
    if lock is None:
        lock = runner._missed_message_ack_lock = asyncio.Lock()
    async with lock:
        try:
            await _run_missed_message_ack(runner, now=now, platform_only=platform_only)
        except Exception:
            logger.warning("Missed-inbound recovery run failed", exc_info=True)


async def start_missed_message_ack(runner: Any, *, platform: Any = None) -> None:
    """Schedule recovery after connection; never block startup or reconnect."""
    try:
        name = str(getattr(platform, "value", platform)) if platform is not None else None
        task = asyncio.create_task(run_missed_message_ack(runner, platform_only=name),
                                   name="missed-inbound-recovery")
        retain = getattr(runner, "_retain_background_task", None)
        if callable(retain):
            retain(task)
    except Exception:
        logger.warning("Could not start missed-inbound recovery", exc_info=True)
