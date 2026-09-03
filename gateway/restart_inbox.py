"""Durable inbox for inbound messages accepted during gateway restart drain.

A restart acknowledgement is truthful only after the normalized event is committed.
Rows remain replayable while ``pending``/``attempting``. Once an agent turn owns a
durable active-turn marker, the row becomes ``handed_off`` and restart recovery owns
continuation, preventing both message replay and turn auto-resume from running it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Optional

from hermes_constants import get_hermes_home

_LOCK = threading.Lock()
_MAX_ATTEMPTS = 5
_STALE_SECONDS = 24 * 60 * 60
_TERMINAL_RETENTION_SECONDS = 7 * 24 * 60 * 60


def _db_path():
    return get_hermes_home() / "state.db"


def _owner_stamp() -> tuple[int, Optional[int]]:
    pid = os.getpid()
    try:
        from gateway.status import get_process_start_time

        return pid, get_process_start_time(pid)
    except Exception:
        return pid, None


def _owner_alive(pid: Any, started_at: Any) -> bool:
    if not pid:
        return False
    try:
        from gateway.status import _pid_exists, get_process_start_time

        pid = int(pid)
        current = get_process_start_time(pid)
        if current is None:
            return bool(_pid_exists(pid))
        return started_at is None or int(current) == int(started_at)
    except Exception:
        return False


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (restart_inbox)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS restart_inbox (
            queue_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            platform TEXT NOT NULL,
            adapter_profile TEXT NOT NULL DEFAULT 'default',
            event_json TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    conn.row_factory = sqlite3.Row
    return conn


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return None


def serialize_event(event: Any) -> str:
    source = event.source
    payload = {
        "text": event.text,
        "message_type": getattr(event.message_type, "value", str(event.message_type)),
        "user_id": event.user_id,
        "user_name": event.user_name,
        "source": source.to_dict(),
        "message_id": event.message_id,
        "platform_update_id": event.platform_update_id,
        "media_urls": list(event.media_urls or []),
        "media_types": list(event.media_types or []),
        "media_text_inlined": list(event.media_text_inlined or []),
        "reply_to_message_id": event.reply_to_message_id,
        "reply_to_text": event.reply_to_text,
        "reply_to_author_id": event.reply_to_author_id,
        "reply_to_author_name": event.reply_to_author_name,
        "reply_to_is_own_message": event.reply_to_is_own_message,
        "prompt_response": _json_safe(event.prompt_response),
        "auto_skill": event.auto_skill,
        "channel_prompt": event.channel_prompt,
        "turn_reasoning_config": _json_safe(event.turn_reasoning_config),
        "turn_reasoning_notice": event.turn_reasoning_notice,
        "channel_context": event.channel_context,
        "internal": event.internal,
        "metadata": _json_safe(event.metadata) or {},
        "timestamp": event.timestamp.isoformat(),
        "allow_gateway_control": event.allow_gateway_control,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def deserialize_event(payload: str):
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource

    data = json.loads(payload)
    timestamp = datetime.fromisoformat(data["timestamp"])
    return MessageEvent(
        text=data["text"],
        message_type=MessageType(data["message_type"]),
        user_id=data.get("user_id"),
        user_name=data.get("user_name"),
        source=SessionSource.from_dict(data["source"]),
        message_id=data.get("message_id"),
        platform_update_id=data.get("platform_update_id"),
        media_urls=list(data.get("media_urls") or []),
        media_types=list(data.get("media_types") or []),
        media_text_inlined=list(data.get("media_text_inlined") or []),
        reply_to_message_id=data.get("reply_to_message_id"),
        reply_to_text=data.get("reply_to_text"),
        reply_to_author_id=data.get("reply_to_author_id"),
        reply_to_author_name=data.get("reply_to_author_name"),
        reply_to_is_own_message=bool(data.get("reply_to_is_own_message")),
        prompt_response=data.get("prompt_response"),
        auto_skill=data.get("auto_skill"),
        channel_prompt=data.get("channel_prompt"),
        turn_reasoning_config=data.get("turn_reasoning_config"),
        turn_reasoning_notice=data.get("turn_reasoning_notice"),
        channel_context=data.get("channel_context"),
        internal=bool(data.get("internal")),
        metadata=dict(data.get("metadata") or {}),
        timestamp=timestamp,
        allow_gateway_control=bool(data.get("allow_gateway_control", True)),
    )


def _queue_id(session_key: str, event: Any) -> str:
    source = event.source
    stable_ref = event.message_id or event.platform_update_id or event.timestamp.isoformat()
    raw = f"{session_key}|{source.platform.value}|{stable_ref}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


def record_event(session_key: str, event: Any, adapter_profile: Optional[str] = None) -> str:
    queue_id = _queue_id(session_key, event)
    now = time.time()
    pid, started = _owner_stamp()
    payload = serialize_event(event)
    platform = event.source.platform.value
    profile = str(adapter_profile or "default")
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO restart_inbox
                       (queue_id, session_key, platform, adapter_profile,
                        event_json, state, attempts, owner_pid, owner_started_at,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                       ON CONFLICT(queue_id) DO NOTHING""",
                    (queue_id, session_key, platform, profile, payload,
                     pid, started, now, now),
                )
                conn.execute(
                    """DELETE FROM restart_inbox
                       WHERE state IN ('handed_off', 'delivered', 'abandoned')
                         AND updated_at < ?""",
                    (now - _TERMINAL_RETENTION_SECONDS,),
                )
        finally:
            conn.close()
    return queue_id


def claim_recoverable(*, deliverable_targets: set[tuple[str, str]]) -> list[dict[str, Any]]:
    now = time.time()
    pid, started = _owner_stamp()
    claimed: list[dict[str, Any]] = []
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                rows = conn.execute(
                    """SELECT * FROM restart_inbox
                       WHERE state IN ('pending', 'attempting')"""
                ).fetchall()
                for row in rows:
                    if _owner_alive(row["owner_pid"], row["owner_started_at"]):
                        continue
                    if (row["platform"], row["adapter_profile"]) not in deliverable_targets:
                        continue
                    if row["attempts"] >= _MAX_ATTEMPTS or now - row["created_at"] > _STALE_SECONDS:
                        conn.execute(
                            "UPDATE restart_inbox SET state='abandoned', updated_at=? WHERE queue_id=?",
                            (now, row["queue_id"]),
                        )
                        continue
                    cursor = conn.execute(
                        """UPDATE restart_inbox
                           SET state='attempting', attempts=attempts+1,
                               owner_pid=?, owner_started_at=?, updated_at=?
                           WHERE queue_id=? AND state=? AND attempts=?
                             AND owner_pid IS ? AND owner_started_at IS ?""",
                        (
                            pid,
                            started,
                            now,
                            row["queue_id"],
                            row["state"],
                            row["attempts"],
                            row["owner_pid"],
                            row["owner_started_at"],
                        ),
                    )
                    if cursor.rowcount:
                        event = deserialize_event(row["event_json"])
                        setattr(event, "_restart_inbox_queue_id", row["queue_id"])
                        claimed.append({
                            "queue_id": row["queue_id"],
                            "session_key": row["session_key"],
                            "platform": row["platform"],
                            "profile": row["adapter_profile"],
                            "event": event,
                        })
        finally:
            conn.close()
    return claimed


def _mark(queue_id: str, state: str) -> bool:
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE restart_inbox SET state=?, updated_at=? WHERE queue_id=?",
                    (state, time.time(), queue_id),
                )
            return bool(cursor.rowcount)
        finally:
            conn.close()


def mark_handed_off(queue_id: str) -> bool:
    return _mark(queue_id, "handed_off")


def release_claim(queue_id: str) -> bool:
    """Return an unhanded claim to pending after dispatch failed."""
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    """UPDATE restart_inbox
                       SET state='pending', owner_pid=NULL, owner_started_at=NULL,
                           updated_at=?
                       WHERE queue_id=? AND state='attempting'""",
                    (time.time(), queue_id),
                )
            return bool(cursor.rowcount)
        finally:
            conn.close()


def mark_delivered(queue_id: str) -> bool:
    return _mark(queue_id, "delivered")
