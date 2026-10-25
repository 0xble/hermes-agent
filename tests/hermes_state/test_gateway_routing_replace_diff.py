"""A full routing-index save writes only what changed, with full-replace semantics.

Every session update full-saves the gateway routing index. Rewriting each of the scope's rows
(about 2,400 on the live store) per update multiplied WAL traffic under the shared writer lock.
"""

from __future__ import annotations

import pytest

from hermes_state import SessionDB


def _rows(db, scope):
    with db._read_ctx() as conn:
        return {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT session_key, entry_json, updated_at FROM gateway_routing WHERE scope = ?", (scope,))}


def test_replace_writes_only_the_difference_and_keeps_replace_semantics(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.replace_gateway_routing_entries({"a": '{"session_id":"1"}', "b": '{"session_id":"2"}', "c": '{"session_id":"3"}'}, scope="s")
        db.replace_gateway_routing_entries({"z": '{"session_id":"other"}'}, scope="other-scope")
        before = _rows(db, "s")
        with db._lock:
            changes_before = db._conn.total_changes

        db.replace_gateway_routing_entries({"a": '{"session_id":"1"}', "b": '{"session_id":"2-new"}', "d": '{"session_id":"4"}'}, scope="s")

        with db._lock:
            changed_rows = db._conn.total_changes - changes_before
        after = _rows(db, "s")
        assert {k: v[0] for k, v in after.items()} == {"a": '{"session_id":"1"}', "b": '{"session_id":"2-new"}', "d": '{"session_id":"4"}'}
        assert after["a"] == before["a"], "an unchanged row must not be rewritten"
        assert changed_rows == 3  # delete c, update b, insert d
        assert _rows(db, "other-scope")["z"][0] == '{"session_id":"other"}'

        db.replace_gateway_routing_entries({}, scope="s")
        assert _rows(db, "s") == {}
    finally:
        db.close()


class _FetchedRows(list):
    def fetchall(self):
        return list(self)

    def fetchone(self):
        return self[0] if self else None


class _CountingConn:
    """Counts Python<->SQLite row round trips made by a write callback. Each fetched row and each
    executemany parameter set is one GIL release/re-take while the writer lock is held."""

    def __init__(self, conn):
        self._conn = conn
        self.round_trips = 0

    def execute(self, sql, params=()):
        self.round_trips += 1
        cursor = self._conn.execute(sql, params)
        rows = cursor.fetchall()
        self.round_trips += len(rows)
        return _FetchedRows(rows)

    def executemany(self, sql, seq):
        seq = list(seq)
        self.round_trips += len(seq)
        return self._conn.executemany(sql, seq)


def test_replace_round_trips_under_the_writer_lock_do_not_grow_with_the_index(tmp_path):
    """Under ~120 busy agent threads each round trip waits a GIL switch interval, so a per-row
    diff of the live 2,753-row index held the writer lock 3-17 s. The diff must stay in SQLite."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        entries = {f"agent:main:slack:dm:{i}": f'{{"session_id": "s{i}"}}' for i in range(2000)}
        db.replace_gateway_routing_entries(entries, scope="s")
        entries["agent:main:slack:dm:7"] = '{"session_id": "changed"}'
        del entries["agent:main:slack:dm:9"]
        entries["agent:main:slack:dm:new"] = '{"session_id": "new"}'

        counted = []
        execute_write = db._execute_write

        def _counting_execute_write(fn, *args, **kwargs):
            def _wrapped(conn):
                proxy = _CountingConn(conn)
                counted.append(proxy)
                return fn(proxy)
            return execute_write(_wrapped, *args, **kwargs)

        db._execute_write = _counting_execute_write
        db.replace_gateway_routing_entries(entries, scope="s")
        del db._execute_write

        assert counted and counted[-1].round_trips <= 4, counted[-1].round_trips
        stored = {k: v[0] for k, v in _rows(db, "s").items()}
        assert stored == entries
    finally:
        db.close()


def test_replace_rejects_an_unencodable_key_instead_of_storing_invalid_utf8(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        with pytest.raises(UnicodeEncodeError):
            db.replace_gateway_routing_entries({"x\ud800": "1"}, scope="s")
        assert db.load_gateway_routing_entries(scope="s") == {}
    finally:
        db.close()
