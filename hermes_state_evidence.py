"""Narrow, non-folding source projections for session evidence streams.

Only explicit allowlists cross the SQLite/Python boundary. Never use conversation loaders here:
they can fold audit identities, read model replay fields or initialize display indexes.
"""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from typing import Generator

# A row may carry a rich user/assistant turn, but not an unbounded attachment or transcript.
# octet_length(column) uses SQLite's stored length without materializing oversized TEXT/BLOBs.
MAX_SOURCE_ROW_BYTES = 256 * 1024
GRAPH_COLUMNS = (
    "id", "parent_session_id", "source", "created_source", "profile_name", "transport_profile",
    "user_id", "session_key", "chat_id", "chat_type", "thread_id", "started_at", "ended_at",
    "end_reason", "last_activity_at", "hidden", "archived", "auto_archived", "pinned",
    "message_count", "tool_call_count", "rewind_count", "runtime_revision", "runtime_generation",
    "transcript_revision",
)
SESSION_ORIGIN_COLUMNS = (
    "id", "parent_session_id", "source", "created_source", "profile_name", "transport_profile",
    "user_id", "session_key", "chat_id", "chat_type", "thread_id", "origin_json",
)
AUDIT_COLUMNS = (
    "id", "session_id", "message_uid", "absorbed_message_uids", "role", "timestamp", "content",
    "active", "compacted", "_compressed_summary", "platform_message_id", "display_order",
    "display_kind", "display_metadata",
)


class EvidenceUnavailable(ValueError):
    """The source cannot satisfy the exact evidence contract, with no successful footer."""


@contextmanager
def evidence_snapshot(db_path):
    """Reuse SessionDB's guarded tracked mode=ro opener, without FTS/schema probing.

    SessionDB's general-purpose constructor prepares SELECT * on FTS tables. An evidence
    projection must not even select those unrelated content columns, so it owns this narrower
    transaction using the same connection/isolation primitives, never a writer or healer.
    """
    from hermes_state import _connect_tracked_db, _ensure_test_isolation, _READ_BUSY_TIMEOUT_S
    from hermes_state_holders import read_only_db_uri
    from hermes_loop_guard import check as check_loop_thread

    check_loop_thread("session evidence snapshot")
    _ensure_test_isolation(db_path)
    conn = _connect_tracked_db(read_only_db_uri(db_path), tracking_path=db_path, uri=True,
                               timeout=_READ_BUSY_TIMEOUT_S, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.rollback()
    finally:
        conn.close()


def _bounded_projection(table: str, columns: tuple[str, ...]) -> str:
    # All identifiers come from module-owned tuples. CASE is evaluated by SQLite before values
    # reach Python. Fetching first and measuring later would already allocate a huge payload.
    fields = [f"{table}.{name}" for name in columns]
    size = " + ".join(f"COALESCE(octet_length({field}), 0)" for field in fields)
    fits = f"({size}) <= {MAX_SOURCE_ROW_BYTES}"
    projection = ", ".join(f"CASE WHEN {fits} THEN {field} END AS {name}"
                           for name, field in zip(columns, fields))
    # Pin the native session/id index: stale planner statistics must not sort whole bodies.
    source = f"{table} INDEXED BY idx_messages_session_id" if table == "messages" else table
    return f"SELECT {fits} AS _fits, {projection} FROM {source}"


def _rows(conn: sqlite3.Connection, table: str, columns: tuple[str, ...],
          suffix: str, params: tuple = ()) -> Generator[dict, None, None]:
    cursor = conn.execute(_bounded_projection(table, columns) + suffix, params)
    try:
        while (row := cursor.fetchone()) is not None:
            if not row["_fits"]:
                raise EvidenceUnavailable(f"{table} row exceeds {MAX_SOURCE_ROW_BYTES} source bytes")
            result = {name: row[name] for name in columns}
            if any(not isinstance(value, (str, int, float, type(None))) for value in result.values()):
                raise EvidenceUnavailable(f"{table} row has an unsupported value type")
            yield result
    finally:
        cursor.close()


def inventory_rows(conn: sqlite3.Connection) -> Generator[dict, None, None]:
    """All structural nodes, including foreign-profile rows and unresolved raw edges."""
    return _rows(conn, "sessions", GRAPH_COLUMNS, " ORDER BY sessions.id COLLATE BINARY")


def exact_session(conn: sqlite3.Connection, session_id: str) -> dict:
    rows = _rows(conn, "sessions", SESSION_ORIGIN_COLUMNS,
                 " WHERE sessions.id = ? COLLATE BINARY", (session_id,))
    try:
        row = next(rows, None)
        if row is None:
            raise EvidenceUnavailable("exact session ID not found in the selected home")
        return row
    finally:
        rows.close()


def audit_rows(conn: sqlite3.Connection, session_id: str) -> Generator[dict, None, None]:
    """Read one physical segment, not display lineage or model replay, in native row-ID order."""
    return _rows(conn, "messages", AUDIT_COLUMNS,
                 " WHERE messages.session_id = ? COLLATE BINARY AND messages.role IN ('user', 'assistant')"
                 " ORDER BY messages.id", (session_id,))


def validate_projection(conn: sqlite3.Connection, transcript: bool) -> None:
    """Fail before emitting the header on stale schemas or SQLite without octet_length."""
    projections: list[tuple[str, tuple[str, ...]]] = [
        ("sessions", SESSION_ORIGIN_COLUMNS if transcript else GRAPH_COLUMNS)]
    if transcript:
        projections.append(("messages", AUDIT_COLUMNS))
    for table, columns in projections:
        conn.execute(_bounded_projection(table, columns) + " LIMIT 0").close()


def audit_state(row: dict) -> str:
    if row["active"] == 1:
        return "active"
    if row["active"] == 0 and row["compacted"] == 1:
        return "compaction_archive"
    if row["active"] == 0 and row["compacted"] == 0:
        return "rewind_excluded"
    return "unknown"
