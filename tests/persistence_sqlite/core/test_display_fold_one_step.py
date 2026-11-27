"""The display-fold scans fetch each page in one SQLite step with exact values and types.

Each row a cursor yields releases and re-takes the GIL; on the loaded gateway every re-take
waits a switch interval, so a ~17k-row compaction plan took 80 s behind one CPU-bound thread.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3

from hermes_state_display_fold import _DISPLAY_FOLD_COLUMNS_SQL, fold_rows
from hermes_state_identity import display_identity


def _table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, role, content, timestamp, tool_call_id, tool_calls, "
        "tool_name, display_kind, display_metadata, display_order, display_identity)")
    rng = random.Random(7)
    values = [None, "", "é \x00\x1f\"\\ 😀 text", "x" * 5000, b"\x00\xff" * 20, b"", 0, -1, 2**63 - 1,
              -(2**63), 1.0, 0.1, 1 / 3, 2.0**-1074, 1.7976931348623157e308, 0.0, -0.0, float(2**53 + 1)]
    for row_id in range(1, 400):
        cells = [rng.choice(values) if rng.random() < 0.5 else rng.uniform(1.6e9, 1.8e9) for _ in range(10)]
        conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", (row_id, *cells))
    return conn


def test_one_step_rows_equal_cursor_rows_in_value_type_and_order():
    conn = _table()
    expected = [dict(row) for row in conn.execute(f"{_DISPLAY_FOLD_COLUMNS_SQL} ORDER BY id")]
    got = fold_rows(conn, f"{_DISPLAY_FOLD_COLUMNS_SQL} WHERE id > ? ORDER BY id DESC", (0,))
    assert [row["id"] for row in got] == [row["id"] for row in expected]
    for want, have in zip(expected, got):
        assert {k: (type(v), repr(v)) for k, v in have.items()} == {k: (type(v), repr(v)) for k, v in want.items()}


class _CountingConn:
    def __init__(self, conn):
        self._conn = conn
        self.rows_stepped = 0

    def execute(self, sql, params=()):
        rows = self._conn.execute(sql, params).fetchall()
        self.rows_stepped += len(rows)
        return _Rows(rows)


class _Rows(list):
    def fetchone(self):
        return self[0] if self else None


def test_a_page_without_real_zeros_is_one_row_step():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, role, content, timestamp, tool_call_id, tool_calls, "
        "tool_name, display_kind, display_metadata, display_order, display_identity)")
    conn.executemany("INSERT INTO messages (id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
                     [(i, f"m{i}", 1.7e9 + i) for i in range(1, 1001)])
    counting = _CountingConn(conn)
    rows = fold_rows(counting, f"{_DISPLAY_FOLD_COLUMNS_SQL} WHERE id > ?", (0,))
    assert len(rows) == 1000
    assert counting.rows_stepped == 1


def test_display_identity_is_plain_sha256_of_the_key_repr():
    for size in (0, 1, 2046, 2047, 2048, 4095, 70_000):
        key = ("user", "y" * size, 1.7e9, None, None, None)
        assert display_identity(key) == hashlib.sha256(repr(key).encode("utf-8", "surrogatepass")).digest()


def test_in_lock_display_index_is_one_row_step_per_index(tmp_path):
    """The fallback fold's in-lock index enumeration held the writer 5-9 s on 19k-row sessions."""
    from pathlib import Path

    from hermes_state import SessionDB

    db = SessionDB(db_path=Path(tmp_path) / "state.db")
    try:
        sid = db.create_session("s1", source="cli")
        with db._lock:
            conn = db._conn
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO messages (session_id, role, content, timestamp, active, display_identity, display_order) "
                "VALUES (?, 'user', ?, ?, 1, ?, ?)",
                [(sid, f"m{i}", 1.7e9 + i, display_identity(("m", i)) if i % 3 else None, i if i % 5 else None)
                 for i in range(1, 801)])
            conn.execute("COMMIT")
            expected = sorted(tuple(row) for row in conn.execute(
                "SELECT id, display_identity, display_order FROM messages WHERE session_id = ?", (sid,)))
            counting = _CountingConn(conn)
            got = db._visible_display_index(counting, sid)
        assert [tuple(row) for row in got] == expected
        assert counting.rows_stepped == 2
    finally:
        db.close()
