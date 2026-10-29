"""Bounded synchronous message insertion, including the existing FTS triggers."""

from __future__ import annotations

import sqlite3
import time

from agent.message_metadata import stamp_message_uid
from hermes_state_display_fold import one_step_rows
from hermes_state_write_metrics import add_write_rows, write_phase

_INSERT_PAGE_ROWS = 200
_INSERT_PAGE_BYTES = 1024 * 1024


def _bound_bytes(params):
    return sum(len(value.encode("utf-8")) if isinstance(value, str) else len(value)
               if isinstance(value, (bytes, bytearray, memoryview)) else 8 for value in params if value is not None)


def _insert_page(conn, sql, session_id, page):
    # Query actual generated ids rather than assuming a contiguous AUTOINCREMENT
    # range (sqlite_sequence can be ahead of MAX(id)). BEGIN IMMEDIATE excludes
    # other writers, and the shipping triggers never insert messages themselves.
    before = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
    prefix, values = sql.split("VALUES", 1)
    with write_phase("insert_sql_fts", thread_cpu=True):
        conn.execute(prefix + "VALUES" + ",".join(values.strip() for _ in page),
                     tuple(value for _, params in page for value in params))
    ids = sorted(one_step_rows(
        conn, "SELECT id, message_uid FROM messages WHERE session_id = ? AND id > ?",
        (session_id, before), ("id", "message_uid")))
    # SQLite allocates increasing rowids in VALUES order. Check both cardinality
    # and logical identities before publishing caller markers. Do not silently
    # adopt ids if an unsupported message-producing trigger changes that contract.
    if len(ids) != len(page) or any(uid != msg["message_uid"] for (_, uid), (msg, _) in zip(ids, page)):
        raise RuntimeError("message insertion identity mismatch")
    for (row_id, _), (msg, _) in zip(ids, page):
        msg["_row_id"] = row_id


def insert_message_rows(db, conn, session_id, messages, *, prune_checkpoints=True):
    """Keep FTS and row publication atomic, with bounded Python/SQLite crossings."""
    from agent.transcript_repair import stamp_inserted_row_snapshots
    from hermes_state_messages import _INSERT_MESSAGE_SQL, _coerce_timestamp, _parse_tool_calls, _tool_calls_count

    now_ts = time.time()
    inserted = tool_calls_total = 0
    batch_tool_index = {}
    page, page_bytes = [], 0
    columns = _INSERT_MESSAGE_SQL.count("?")
    row_limit = min(_INSERT_PAGE_ROWS, conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) // columns)
    if row_limit < 1:
        raise sqlite3.OperationalError("too many SQL variables")
    for msg in messages:
        with write_phase("insert_prepare", thread_cpu=True):
            role = msg.get("role", "unknown")
            tool_calls = _parse_tool_calls(msg.get("tool_calls"))
            message_timestamp = _coerce_timestamp(msg.get("timestamp"), now_ts)
            stamp_message_uid(msg)
            db._stamp_tool_call_uids(msg, tool_calls, batch_tool_index)
            params = db._message_row_params(
                session_id, role, msg, tool_calls, message_timestamp, keep_reasoning=role == "assistant")
            size = _bound_bytes(params)
        if page and (len(page) >= row_limit or page_bytes + size > _INSERT_PAGE_BYTES):
            _insert_page(conn, _INSERT_MESSAGE_SQL, session_id, page)
            page, page_bytes = [], 0
        msg["timestamp"] = message_timestamp
        page.append((msg, params))
        page_bytes += size
        inserted += 1
        tool_calls_total += _tool_calls_count(tool_calls)
        now_ts = max(now_ts, message_timestamp) + 1e-6
    if page:
        _insert_page(conn, _INSERT_MESSAGE_SQL, session_id, page)
    if prune_checkpoints:
        with write_phase("insert_checkpoints", thread_cpu=True):
            db._prune_shadowed_checkpoints(conn, session_id, messages)
    # Hash the stored, affinity-coerced values after checkpoint pruning, as before.
    with write_phase("insert_snapshots", thread_cpu=True):
        stamp_inserted_row_snapshots(conn, session_id, messages)
    add_write_rows("inserted", inserted)
    return inserted, tool_calls_total
