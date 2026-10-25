"""Cross-process busy-session routing for zero-cut gateway handover.

The local adapter queue remains the source of FIFO ordering. This module only adds the durable
owner read and the watcher needed when the owner lives in another gateway process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

from gateway.config import is_zero_cut_handover_enabled

logger = logging.getLogger("gateway.zero_cut_busy")
_NOT_HANDLED: Any = object()


def _enabled(runner: Any) -> bool:
    return bool(is_zero_cut_handover_enabled(getattr(runner, "config", None)))


def _raw_store(store: Any) -> Any:
    return getattr(store, "_store", store)


def _marker(store: Any, session_key: str) -> Optional[dict[str, Any]]:
    """Read one marker row directly from state.db; never consult SessionStore._entries."""
    store = _raw_store(store)
    db = getattr(store, "_routing_db", None)
    loader = getattr(db, "load_gateway_routing_entry", None)
    if not callable(loader):
        return None
    raw = loader(session_key, scope=store._routing_scope())
    if not raw:
        return None
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("active_turn_token")
    pid = payload.get("active_turn_owner_pid")
    slot = payload.get("active_turn_owner_slot")
    if not token or not isinstance(pid, int) or pid <= 0 or not isinstance(slot, str) or not slot:
        return None
    return {"token": token, "pid": pid, "slot": slot}


def read_active_turn_marker(store: Any, session_key: str) -> Optional[dict[str, Any]]:
    """Public seam for tests and off-loop callers that need one durable marker."""
    return _marker(store, session_key)


def clear_stale_active_turn_marker(store: Any, session_key: str, token: str) -> bool:
    store = _raw_store(store)
    clear = getattr(getattr(store, "_routing_db", None), "clear_gateway_routing_active_turn", None)
    if not callable(clear):
        return False
    return bool(clear(session_key, token, scope=store._routing_scope()))


def _owner_live(pid: int) -> bool:
    from gateway.status import _pid_exists
    return bool(_pid_exists(pid))


def _event_id(event: Any) -> Any:
    return getattr(event, "message_id", None) or getattr(getattr(event, "source", None), "message_id", None)


def _log_route(*, session_key: str, event: Any, marker: Optional[dict[str, Any]], action: str) -> None:
    fields = {
        "session_key": session_key,
        "inbound_message_id": _event_id(event),
        "owner_pid": marker.get("pid") if marker else None,
        "owner_slot": marker.get("slot") if marker else None,
        "active_turn_token": marker.get("token") if marker else None,
        "action": action,
        "successor_turn_id": _event_id(event),
    }
    logger.info("zero-cut busy routing %s", action, extra=fields)


def _pending_for(runner: Any, session_key: str):
    adapter = None
    pending = None
    for candidate in (getattr(runner, "adapters", {}) or {}).values():
        slot = getattr(candidate, "_pending_messages", None)
        if isinstance(slot, dict) and session_key in slot:
            adapter, pending = candidate, slot[session_key]
            break
    return adapter, pending


async def handle_local_busy(runner: Any, event: Any, source: Any, session_key: str) -> Any:
    """Run the historical local busy fast path after foreign ownership was ruled out."""
    runner._hm_evict_idle_stale_agent(session_key)
    if runner._is_session_running(session_key):
        await runner._hm_evict_reaped_agent(session_key)
    if runner._is_session_running(session_key):
        return await runner._hm_handle_running_session_message(event, source, session_key)
    return _NOT_HANDLED


async def queue_foreign_owner(runner: Any, event: Any, session_key: str) -> bool:
    """Queue an inbound event behind a live foreign turn, without local admission.

    A key already waiting in the watcher keeps queueing even after the marker clears, so a newer
    message never jumps ahead of an older one in the gap before the watcher drains it.
    """
    if not _enabled(runner):
        return False
    waiting = session_key in (getattr(runner, "_zero_cut_busy_keys", None) or ())
    marker = None
    if not (waiting and _pending_for(runner, session_key)[1] is not None):
        marker = await asyncio.to_thread(_marker, runner.async_session_store, session_key)
        if marker is None or marker["pid"] == os.getpid() or not _owner_live(marker["pid"]):
            return False
    schedule_activity = getattr(runner, "_schedule_attended_user_activity", None)
    if callable(schedule_activity):
        schedule_activity(event, event.source, session_key)
    accepted = runner._queue_or_replace_pending_event(session_key, event)
    if accepted:
        keys = getattr(runner, "_zero_cut_busy_keys", None)
        if keys is None:
            keys = runner._zero_cut_busy_keys = set()
        keys.add(session_key)
        _log_route(session_key=session_key, event=event, marker=marker, action="queued")
    return bool(accepted)


async def watch_foreign_queues(runner: Any) -> None:
    """Poll queued foreign-owner keys until shutdown; one-second cadence, no wait budget."""
    while True:
        await asyncio.sleep(1.0)
        keys = getattr(runner, "_zero_cut_busy_keys", None)
        if not keys:
            continue
        for session_key in tuple(keys):
            adapter, pending = _pending_for(runner, session_key)
            if adapter is None or pending is None:
                keys.discard(session_key)
                continue
            marker = await asyncio.to_thread(_marker, runner.async_session_store, session_key)
            stale_cleared = False
            if marker is not None and marker["pid"] != os.getpid() and not _owner_live(marker["pid"]):
                raw_store = _raw_store(runner.async_session_store)
                clear = getattr(getattr(raw_store, "_routing_db", None),
                                "clear_gateway_routing_active_turn", None)
                if callable(clear):
                    stale_cleared = await asyncio.to_thread(
                        clear, session_key, marker["token"],
                        scope=raw_store._routing_scope(),
                    )
                if stale_cleared:
                    _log_route(session_key=session_key, event=pending, marker=marker, action="stale_cleared")
                marker = await asyncio.to_thread(_marker, runner.async_session_store, session_key)
            if marker is not None:
                continue
            # No local turn may own the chat: a live local guard drains its own pending slot when its
            # turn ends, so leave it there. Otherwise start the follow-up exactly like a fresh inbound
            # (guard + background task) on the adapter that holds it.
            if session_key in (getattr(adapter, "_active_sessions", None) or {}):
                keys.discard(session_key)
                continue
            pending = adapter._pending_messages.pop(session_key, None)
            if pending is None:
                keys.discard(session_key)
                continue
            if adapter._start_session_processing(pending, session_key):
                keys.discard(session_key)
                _log_route(session_key=session_key, event=pending, marker=None, action="drained")
            else:
                adapter._pending_messages.setdefault(session_key, pending)  # keep it for the next poll


class ZeroCutBusyRoutingMixin:
    """Thin runner hook for the handover-only watcher."""

    def _start_zero_cut_busy_watcher(self) -> None:
        if _enabled(self):
            self._zero_cut_busy_keys = set()
            self._spawn_supervised(self._zero_cut_busy_watcher, "zero_cut_busy_watcher")

    async def _zero_cut_busy_watcher(self) -> None:
        await watch_foreign_queues(self)
