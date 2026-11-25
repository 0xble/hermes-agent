"""Answer messages that arrived while a platform was disconnected.

When a primary adapter is published (gateway start, or a platform reconnect), the adapter is
asked for the human messages it missed in the offline interval. Each one is handed to the
adapter's own inbound handler, so authorization, mention rules, gating, session routing and
threading are exactly those of a live message. The agent sees a note saying the message is late.

The offline interval runs from a per-platform watermark (the last moment the platform was known
connected) to a few seconds after it connected again, capped at 24 hours. Those seconds let the
transport finish connecting: a message that arrives live in them is claimed live and not replayed. A platform with no watermark has
never been connected, so there is nothing to recover. Each message is claimed durably before
dispatch and the claim is never released: a message is answered at most once, also across
restarts and crash loops. Controlled by ``gateway.missed_inbound_recovery.enabled``.

An adapter opts in by duck typing:

- ``fetch_missed_inbound(since, until)`` returns records with ``conversation`` and ``ts`` (plus
  whatever it needs to replay them, and optional ``reply_metadata`` for the overflow summary).
- ``dispatch_missed_inbound(record, note)`` feeds one record to its inbound handler.
- ``missed_inbound_live_until()`` (optional) returns the moment up to which the transport is
  proven connected, ``False`` while it is down, or None when that is unknown.
- ``mark_missed_inbound_recovered(conversation, ts)`` (optional) learns which messages were
  recovered, so a platform replay of the same message is dropped on the live path.
- ``missed_inbound_observer`` and ``missed_inbound_reconnected`` are set here: the adapter calls
  the first for every live message it dispatches and the second when its transport reconnects.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import defaultdict
from datetime import datetime
from functools import partial
from typing import Any

logger = logging.getLogger(__name__)

MAX_WINDOW_SECONDS = 24 * 60 * 60
MAX_DISPATCH_PER_SCAN = 20
HEARTBEAT_SECONDS = 30.0
RECONNECT_SETTLE_SECONDS = 5.0
_CLAIM_RETENTION_SECONDS = 7 * 24 * 60 * 60
_PRUNE_EVERY_SECONDS = 60 * 60


class _State:
    """Per-runner bookkeeping: scans in flight per platform, and the heartbeat task."""

    def __init__(self) -> None:
        self.scanning: dict[str, int] = defaultdict(int)
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.settling: set[str] = set()  # a reconnect scan is waiting for the transport to settle
        self.down: set[str] = set()  # the heartbeat saw the transport down
        self.pruned_at = 0.0
        self.heartbeat: asyncio.Task | None = None


def _state(runner: Any) -> _State:
    state = getattr(runner, "_missed_inbound_state", None)
    if not isinstance(state, _State):
        state = runner._missed_inbound_state = _State()
    return state


def _name(platform: Any) -> str:
    return str(getattr(platform, "value", platform))


def _enabled(runner: Any) -> bool:
    # ``is True``: a mocked config must never switch recovery on.
    return getattr(getattr(runner, "config", None), "missed_inbound_recovery_enabled", False) is True


def _db(runner: Any) -> Any:
    """The synchronous SessionDB behind the runner (unwrapping the async door), or None."""
    db = getattr(runner, "_session_db", None)
    db = getattr(db, "_db", db)
    return db if inspect.ismethod(getattr(db, "claim_missed_inbound", None)) else None


def _adapter(runner: Any, name: str) -> Any:
    for platform, adapter in list((getattr(runner, "adapters", None) or {}).items()):
        if _name(platform) == name:
            return adapter
    return None


def _recovers(adapter: Any) -> bool:
    return adapter is not None and callable(getattr(adapter, "fetch_missed_inbound", None))


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _local(at: float) -> str:
    return datetime.fromtimestamp(at).astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()


def late_note(sent_at: float, offline_from: float, offline_to: float) -> str:
    """The context the agent reads in front of a message it is answering late."""
    return (
        f"[Delivered late: sent {_local(sent_at)}, the gateway was offline from {_local(offline_from)} "
        f"to {_local(offline_to)}. Acknowledge the delay, answer what still applies, and confirm before "
        "acting on anything time-sensitive.]")


def schedule_missed_inbound_recovery(runner: Any, platform: Any) -> None:
    """Start a recovery scan for one platform that has just connected. Never raises."""
    try:
        name = _name(platform)
        adapter = _adapter(runner, name)
        if not _enabled(runner) or _db(runner) is None or not _recovers(adapter):
            return
        adapter.missed_inbound_observer = partial(_note_live_inbound, runner, name)
        adapter.missed_inbound_reconnected = partial(_on_transport_reconnect, runner, name)
        _start_scan(runner, name, None)
        state = _state(runner)
        if state.heartbeat is None or state.heartbeat.done():
            state.heartbeat = _spawn(runner, _heartbeat_loop(runner), "missed-inbound-heartbeat")
    except Exception:
        logger.warning("Could not start missed inbound recovery for %s", platform, exc_info=True)


def _on_transport_reconnect(runner: Any, name: str) -> None:
    """The adapter rebuilt its transport in place: scan what the dead one missed, once it settles."""
    # A scan already waiting to settle picks its end time later, so it covers this reconnect too.
    if _enabled(runner) and _db(runner) is not None and name not in _state(runner).settling:
        _start_scan(runner, name, None)


def _spawn(runner: Any, coro: Any, name: str) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro, name=name)
    retain = getattr(runner, "_retain_background_task", None)
    if callable(retain):
        retain(task)
    return task


def _start_scan(runner: Any, name: str, until: float | None) -> asyncio.Task:
    """Scan *name* up to *until*, or (None) up to the moment the transport has settled."""
    # Counted synchronously, before the task runs: the heartbeat must not move the watermark
    # past the offline interval this scan is about to read.
    state = _state(runner)
    state.scanning[name] += 1
    state.down.discard(name)
    if until is None:
        state.settling.add(name)
    return _spawn(runner, _counted_scan(runner, name, until), f"missed-inbound-{name}")


async def _counted_scan(runner: Any, name: str, until: float | None) -> int:
    state = _state(runner)
    try:
        if until is None:
            try:
                await asyncio.sleep(RECONNECT_SETTLE_SECONDS)
            finally:
                state.settling.discard(name)
            until = time.time()
        return await _guarded_scan(runner, name, until)
    finally:
        state.scanning[name] -= 1


async def recover_platform(runner: Any, platform: Any, *, until: float | None = None) -> int:
    """Scan one platform now and return how many messages were dispatched. Never raises."""
    name = _name(platform)
    state = _state(runner)
    state.scanning[name] += 1
    try:
        return await _guarded_scan(runner, name, time.time() if until is None else float(until))
    finally:
        state.scanning[name] -= 1


async def _guarded_scan(runner: Any, name: str, until: float) -> int:
    try:
        async with _state(runner).locks[name]:
            return await _scan(runner, name, until)
    except Exception:
        logger.warning("Missed inbound recovery failed for %s", name, exc_info=True)
        return 0


async def _scan(runner: Any, name: str, until: float) -> int:
    db, adapter = _db(runner), _adapter(runner, name)
    if db is None or not _recovers(adapter) or not _enabled(runner):
        return 0
    watermark = await asyncio.to_thread(db.get_missed_inbound_watermark, name)
    if watermark is None:
        # First connection of this platform: there is no offline interval to recover.
        await asyncio.to_thread(db.advance_missed_inbound_watermark, name, until)
        return 0
    since = max(float(watermark), until - MAX_WINDOW_SECONDS)
    dispatched = 0
    try:
        # A message recovered by an earlier run may still be replayed live by the platform.
        for conversation, ts in await asyncio.to_thread(db.list_recovered_missed_inbound, name):
            _mark_recovered(adapter, conversation, ts)
        await asyncio.to_thread(db.prune_missed_inbound_claims, until - _CLAIM_RETENTION_SECONDS)
        if until > since:
            records = _in_window(await _maybe_await(adapter.fetch_missed_inbound(since, until)), name, since, until)
            if records:
                dispatched = await _dispatch(db, adapter, name, records, since, until)
            if getattr(adapter, "missed_inbound_scan_complete", True) is False:
                # The adapter may have returned partial results after a rate limit or call-budget
                # stop. Claims keep dispatched records idempotent, but the interval must be retried.
                logger.warning("Missed inbound scan incomplete for %s; retaining watermark", name)
                return dispatched
    except asyncio.CancelledError:
        # Preserve the watermark when shutdown interrupts a scan. The next start must rescan
        # the unfinished interval rather than silently losing messages discovered after it.
        logger.info("Missed inbound scan cancelled for %s", name)
        raise
    except Exception:
        # A partial or failed scan must not advance the watermark. The next reconnect can retry
        # the interval, while durable claims prevent messages already dispatched from repeating.
        logger.warning("Missed inbound scan failed for %s", name, exc_info=True)
        return dispatched
    await asyncio.to_thread(db.advance_missed_inbound_watermark, name, until)
    return dispatched


def _in_window(records: Any, name: str, since: float, until: float) -> list[dict[str, Any]]:
    """Well-formed records inside ``(since, until]``, oldest first, one per conversation and ts."""
    kept: dict[tuple[str, str], dict[str, Any]] = {}
    for item in records if isinstance(records, (list, tuple)) else ():
        if not isinstance(item, dict):
            continue
        conversation, ts = str(item.get("conversation") or ""), str(item.get("ts") or "")
        try:
            sent_at = float(ts)
        except ValueError:
            continue
        if conversation and since < sent_at <= until:
            kept.setdefault((conversation, ts), {**item, "platform": name, "conversation": conversation, "ts": ts})
    return sorted(kept.values(), key=lambda item: float(item["ts"]))


def _mark_recovered(adapter: Any, conversation: str, ts: str) -> None:
    mark = getattr(adapter, "mark_missed_inbound_recovered", None)
    if callable(mark):
        mark(conversation, ts)


async def _dispatch(db: Any, adapter: Any, name: str, records: list[dict[str, Any]],
                    since: float, until: float) -> int:
    dispatch = getattr(adapter, "dispatch_missed_inbound", None)
    if not callable(dispatch):
        return 0
    dispatched = 0
    overflow: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:  # oldest first, so every conversation is replayed in order
        conversation, ts = record["conversation"], record["ts"]
        if not await asyncio.to_thread(db.claim_missed_inbound, name, conversation, ts):
            continue
        _mark_recovered(adapter, conversation, ts)
        if dispatched >= MAX_DISPATCH_PER_SCAN:
            overflow[conversation].append(record)
            continue
        dispatched += 1
        try:
            await _maybe_await(dispatch(record, late_note(float(ts), since, until)))
        except Exception:
            # The claim stays: answering twice is worse than missing one message.
            logger.warning("Missed inbound dispatch failed for %s %s %s", name, conversation, ts, exc_info=True)
    for conversation, items in overflow.items():
        await _send_overflow_summary(adapter, name, conversation, items, since, until)
    logger.info("Missed inbound recovery for %s: dispatched %d, summarized %d in %d conversation(s)",
                name, dispatched, sum(len(items) for items in overflow.values()), len(overflow))
    return dispatched


async def _send_overflow_summary(adapter: Any, name: str, conversation: str,
                                 items: list[dict[str, Any]], since: float, until: float) -> None:
    count = len(items)
    text = (
        f"I was offline from {_local(since)} to {_local(until)} and have not answered "
        f"{count} more message{'s' if count != 1 else ''} sent here in that time. "
        "Send again anything you still need.")
    try:
        await _maybe_await(adapter.send(conversation, text, metadata=items[0].get("reply_metadata") or None))
    except Exception:
        logger.warning("Missed inbound summary failed for %s %s", name, conversation, exc_info=True)


def _note_live_inbound(runner: Any, name: str, conversation: str, ts: str) -> None:
    """Claim a message that arrived live, without delaying it: recovery must never replay it."""
    db = _db(runner)
    if db is not None and conversation and ts:
        _spawn(runner, _claim_live(db, name, conversation, ts), "missed-inbound-live-claim")


async def _claim_live(db: Any, name: str, conversation: str, ts: str) -> None:
    try:
        await asyncio.to_thread(partial(db.claim_missed_inbound, name, conversation, ts, live=True))
    except Exception:
        logger.debug("Could not claim live inbound %s %s %s", name, conversation, ts, exc_info=True)


async def _live_until(adapter: Any, now: float) -> float | bool | None:
    """When the transport was last proven connected; False when it is down; None when unknown."""
    probe = getattr(adapter, "missed_inbound_live_until", None)
    if not callable(probe):
        return now if getattr(adapter, "is_connected", False) is True else None
    value = await _maybe_await(probe())
    if value is False:
        return False
    return min(now, float(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


async def heartbeat_once(runner: Any, now: float | None = None) -> None:
    """Move each connected platform's watermark up to the moment it is proven connected.

    A platform whose transport was seen down and is up again is scanned instead: the watermark
    still marks the last moment it was connected, so the scan covers the time it was down.
    """
    db = _db(runner)
    if db is None or not _enabled(runner):
        return
    state = _state(runner)
    now = time.time() if now is None else float(now)
    for platform, adapter in list((getattr(runner, "adapters", None) or {}).items()):
        name = _name(platform)
        if not _recovers(adapter):
            continue
        try:
            live_until = await _live_until(adapter, now)
            # Re-checked after the probe: a scan that started meanwhile owns the watermark.
            if live_until is None or state.scanning[name] > 0:
                continue
            if live_until is False:
                state.down.add(name)
            elif name in state.down:
                _start_scan(runner, name, now)
            else:
                await asyncio.to_thread(db.advance_missed_inbound_watermark, name, live_until)
        except Exception:
            logger.debug("Missed inbound heartbeat failed for %s", name, exc_info=True)
    # Live messages are claimed all the time a platform is up, so claims are dropped here too.
    if now - state.pruned_at >= _PRUNE_EVERY_SECONDS:
        state.pruned_at = now
        try:
            await asyncio.to_thread(db.prune_missed_inbound_claims, now - _CLAIM_RETENTION_SECONDS)
        except Exception:
            logger.debug("Could not prune missed inbound claims", exc_info=True)


async def _heartbeat_loop(runner: Any) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        await heartbeat_once(runner)
