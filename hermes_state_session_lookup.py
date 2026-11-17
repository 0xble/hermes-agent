"""Typed, read-only session identity lookup helpers."""

from __future__ import annotations

from typing import Any, Optional

from hermes_state_common import escape_like as _escape_like
from hermes_state_deletion import visible_session_sql


def lookup_sessions(
    db: Any, *, title: Optional[str] = None, topic_name: Optional[str] = None,
    platform: Optional[str] = None, chat_id: Optional[str] = None,
    thread_id: Optional[str] = None, limit: int = 10,
) -> list[dict[str, Any]]:
    """Bounded, read-only lookup by title/topic or an exact platform route."""
    if not any(value not in (None, "") for value in (title, topic_name, platform, chat_id, thread_id)):
        raise ValueError("lookup requires title, topic_name, platform, chat_id, or thread_id")
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 10
    clauses = [visible_session_sql("s.id")]
    params: list[Any] = []
    if title not in (None, ""):
        clauses.append("LOWER(COALESCE(s.title, '')) LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(str(title).strip().lower())}%")
    if topic_name not in (None, ""):
        topic = str(topic_name).strip().lower()
        topic_clause = "(LOWER(COALESCE(s.title, '')) = ? OR LOWER(COALESCE(s.display_name, '')) = ?)"
        params.extend((topic, topic))
        topic_tables = {row["name"] for row in db._read_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
            "('telegram_dm_topic_bindings', 'telegram_dm_topic_owners')")}
        if topic_tables == {"telegram_dm_topic_bindings", "telegram_dm_topic_owners"}:
            topic_clause = topic_clause[:-1] + " OR EXISTS (SELECT 1 FROM telegram_dm_topic_bindings b "
            "JOIN telegram_dm_topic_owners o ON o.profile_name = b.profile_name "
            "AND o.chat_id = b.chat_id AND o.thread_id = b.thread_id "
            "WHERE b.session_id = s.id AND LOWER(o.topic_name) = ?))"
            params.append(topic)
        clauses.append(topic_clause)
    if platform not in (None, ""):
        clauses.append("(LOWER(COALESCE(s.source, '')) = LOWER(?) OR "
                       "(json_valid(s.origin_json) AND LOWER(COALESCE(json_extract(s.origin_json, '$.platform'), '')) = LOWER(?)))")
        params.extend((str(platform), str(platform)))
    if chat_id not in (None, ""):
        clauses.append("s.chat_id = ?")
        params.append(str(chat_id))
    if thread_id not in (None, ""):
        clauses.append("COALESCE(s.thread_id, '') = ?")
        params.append(str(thread_id))
    rows = db._read_all(
        "SELECT s.id AS session_id, s.title, s.source, s.chat_id, s.chat_type, s.thread_id, "
        "s.session_key, s.display_name, s.profile_name, s.started_at, s.ended_at "
        "FROM sessions s WHERE " + " AND ".join(clauses) +
        " ORDER BY s.started_at DESC LIMIT ?", params + [limit])
    return [dict(row) for row in rows]
