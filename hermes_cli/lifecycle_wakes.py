"""Durable wakes for stall cards that ``goals.notices``/``loops.notices: hidden`` keeps out of chat.

A paused, blocked, failed or tick-budget card that nobody sees could leave a goal or loop
stalled with nobody told. Instead the gateway records the card here, in the profile's
SessionDB ``state_meta``, and the session-control watcher wakes the session with an internal
turn carrying it. The agent then resolves the stall within its authority or tells the user in
its own reply. Records survive restarts. One card text wakes a session at most once per
``REWAKE_AFTER_SECONDS``, so a stall that repeats every turn cannot wake the agent in a loop.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_PREFIX = "lifecycle_wake:"
REWAKE_AFTER_SECONDS = 6 * 60 * 60
# An undeliverable wake (retired or rebound session) settles after this long.
UNDELIVERABLE_AFTER_SECONDS = 24 * 60 * 60


def _db():
    from hermes_cli import goals
    return goals._get_session_db()


def _key(session_id: str, section: str, message: str) -> str:
    digest = hashlib.sha256(f"{section}\0{message}".encode()).hexdigest()[:16]
    return f"{_PREFIX}{session_id}:{digest}"


def record_stall(*, session_key: str, session_id: str, section: str, message: str) -> bool:
    """Queue one wake for this stall card. False when the same card already woke recently."""
    db = _db()
    if db is None or not session_id or not session_key:
        return False
    key, now = _key(session_id, section, message), time.time()

    def write(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is not None:
            try:
                existing = json.loads(row[0])
            except (TypeError, ValueError):
                existing = {}
            if now - float(existing.get("created_at") or 0) < REWAKE_AFTER_SECONDS:
                return False
        db.set_meta(key, json.dumps({
            "key": key, "session_key": session_key, "session_id": session_id, "section": section,
            "message": str(message), "created_at": now, "settled": None,
        }, ensure_ascii=False), cursor=conn)
        return True

    return bool(db._execute_write(write))


def pending() -> list[dict[str, Any]]:
    """Unsettled wakes. Settled rows past the rewake window are deleted here."""
    db = _db()
    if db is None:
        return []
    out, stale, now = [], [], time.time()
    for key, raw in db.list_meta_prefix(_PREFIX):
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            stale.append(key)
            continue
        if record.get("settled"):
            if now - float(record.get("created_at") or 0) >= REWAKE_AFTER_SECONDS:
                stale.append(key)
            continue
        out.append(record)
    if stale:
        db._execute_write(lambda conn: conn.executemany(
            "DELETE FROM state_meta WHERE key = ?", [(key,) for key in stale]))
    return out


def settle(key: str, outcome: str) -> Optional[dict[str, Any]]:
    """Mark a wake ``delivered`` or ``skipped``. The row stays to block a rewake in the window."""
    db = _db()
    if db is None:
        return None

    def write(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        record = json.loads(row[0])
        record["settled"] = outcome
        db.set_meta(key, json.dumps(record, ensure_ascii=False), cursor=conn)
        return record

    return db._execute_write(write)


def is_undeliverable(record: dict[str, Any]) -> bool:
    return time.time() - float(record.get("created_at") or 0) >= UNDELIVERABLE_AFTER_SECONDS


def wake_text(record: dict[str, Any]) -> str:
    section = str(record.get("section") or "goals")
    kind = section[:-1]
    return (f"[{kind.title()} card hidden from the user by {section}.notices: hidden] "
            f"{record.get('message', '')}\n"
            f"This {kind} has stalled. Resolve it within your authority, or tell the user what you "
            "need in your reply. Do not resume or clear anything the user paused or stopped.")
