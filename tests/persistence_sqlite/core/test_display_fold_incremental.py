"""Affected display groups must equal the full fold, including legacy repair work."""

import copy
from contextlib import closing
import json
import random
import re
import sqlite3
import threading

import pytest

from agent.context_compressor import (
    HISTORICAL_TASK_HEADING, MODEL_ONLY_DISPLAY_METADATA_KEY, SUMMARY_PREFIX,
    _MERGED_PRIOR_CONTEXT_HEADER, _MERGED_SUMMARY_DELIMITER, _SUMMARY_END_MARKER,
)
from hermes_state import SessionDB


def _state(db):
    return [tuple(row) for row in db._read_all(
        "SELECT id, display_identity, display_order FROM messages "
        "WHERE session_id = 's' AND (active = 1 OR compacted = 1) ORDER BY id")]


def _projections(db):
    return [db.get_messages("s", include_compacted=True, **kwargs) for kwargs in (
        {}, {"limit": 7}, {"limit": 7, "offset": 3}, {"limit": 7, "latest": True})]


def _assert_full_equivalent(db):
    before = _state(db)
    projection = _projections(db)
    db._execute_write(lambda conn: db._reconcile_display_orders(conn, "s"))
    assert _state(db) == before
    assert _projections(db) == projection
    db._execute_write(lambda conn: db._reconcile_display_orders(conn, "s"))
    assert _state(db) == before


def _fixture(db, seed):
    rng = random.Random(seed)
    db.create_session("s", source="test")
    summary = f"{SUMMARY_PREFIX}\n\n{HISTORICAL_TASK_HEADING}\nold work\n\n{_SUMMARY_END_MARKER}"
    handoff = f"{_MERGED_PRIOR_CONTEXT_HEADER}\nlive ask\n\n{_MERGED_SUMMARY_DELIMITER}\n\n{summary}"
    messages = [
        {"role": "assistant", "content": f"history {i}", "timestamp": 100.0 + i}
        for i in range(32)]
    messages += [
        {"role": "user", "content": handoff, "timestamp": 150.0},
        {"role": "user", "content": "live ask", "timestamp": 150.0},
        {"role": "assistant", "content": None, "timestamp": 151.0,
         "tool_calls": [{"id": "call_a", "type": "function", "function": {
             "name": "terminal", "arguments": json.dumps({"command": "x" * 1000})}}]},
        {"role": "assistant", "content": None, "timestamp": 151.0,
         "tool_calls": [{"id": "call_a", "type": "function", "function": {
             "name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "content": "result", "tool_call_id": "call_a", "timestamp": 152.0},
    ]
    db.append_messages_batch("s", messages)
    ids = [row[0] for row in db._read_all("SELECT id FROM messages ORDER BY id")]

    def generations(conn):
        db._clone_message_rows(conn, ids[:12])
        conn.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = 's'")
        db._clone_message_rows(conn, ids[24:])
        # Reentry and model-only metadata remain fold population members.
        conn.execute("UPDATE messages SET display_metadata = ? WHERE id = ?",
                     (json.dumps({MODEL_ONLY_DISPLAY_METADATA_KEY: True}), ids[4]))
        visible = list(conn.execute("SELECT id, display_identity FROM messages ORDER BY id"))
        aliases = [visible[1][1], visible[2][1], bytes(32)]
        for row_id, identity in visible[:20]:
            choice = rng.randrange(5)
            changes = {
                0: ("display_identity = ?", (rng.choice(aliases), row_id)),
                1: ("display_identity = NULL", (row_id,)),
                2: ("display_order = NULL", (row_id,)),
                3: ("display_order = ?", (row_id + 999, row_id)),
            }
            if choice in changes:
                assignment, values = changes[choice]
                conn.execute(f"UPDATE messages SET {assignment} WHERE id = ?", values)
        # A removed oldest member has peers under both its own and a foreign alias. The visibility
        # trigger and repair_keys cover it; preimage seeding is defence in depth.
        conn.execute("UPDATE messages SET active = 1, compacted = 0 WHERE id = ?", (ids[0],))
        conn.execute("UPDATE messages SET display_identity = ? WHERE id = ?", (bytes(32), ids[0]))

    db._execute_write(generations)


def test_alias_only_repair_requires_stored_alias_expansion(tmp_path):
    states = {}
    for expand_aliases in (True, False):
        with closing(SessionDB(tmp_path / f"aliases-{expand_aliases}.sqlite")) as db:
            db.create_session("s", source="test")
            db.append_messages_batch("s", [
                {"role": "assistant", "content": "same identity", "timestamp": 100.0},
                {"role": "assistant", "content": "same identity", "timestamp": 100.0},
            ])
            ids = [row[0] for row in db._read_all("SELECT id FROM messages ORDER BY id")]

            def corrupt(conn):
                for row_id, alias in zip(ids, (b"a" * 32, b"b" * 32)):
                    conn.execute("UPDATE messages SET display_identity = ?, display_order = 99 WHERE id = ?",
                                 (alias, row_id))

            db._execute_write(corrupt)
            plan = db._display_fold_plan("s")
            assert plan is not None
            assert len(plan.repair_keys) == 1
            if not expand_aliases:
                plan.aliases.clear()  # Negative control: canonical keys match neither stored alias.

            def incremental(conn):
                selection = db._begin_display_fold(conn, plan)
                assert type(selection).__name__ == "_AffectedFold"
                db._reconcile_display_orders(conn, "s", selection)

            db._execute_write(incremental)
            states[expand_aliases] = _state(db)
            db._execute_write(lambda conn: db._reconcile_display_orders(conn, "s"))
            oracle = _state(db)
            assert (states[expand_aliases] == oracle) is expand_aliases
            assert [row[2] for row in oracle] == [min(ids), min(ids)]
    assert [row[2] for row in states[False]] == [99, 99]


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("mode", ["incremental", "no_plan", "missing_index", "missing_backfill", "guard_unavailable", "replacement", "retry"])
def test_randomized_incremental_equals_full_fold(tmp_path, monkeypatch, caplog, named, mode):
    for seed in range(20):
        db = SessionDB(tmp_path / f"case-{seed}.sqlite")
        try:
            _fixture(db, seed)
            db.create_session("foreign", source="test")
            db.append_messages_batch("foreign", [{"role": "assistant", "content": "foreign peer"}] * 2)
            db._write_sql("UPDATE messages SET display_identity = (SELECT display_identity FROM messages "
                          "WHERE session_id = 's' AND id = 2) WHERE session_id = 'foreign'")
            foreign = [tuple(row) for row in db._read_all("SELECT * FROM messages WHERE session_id = 'foreign'")]
            untouched = [tuple(row) for row in db._read_all(
                "SELECT id, display_identity, display_order FROM messages WHERE id BETWEEN 21 AND 24 ORDER BY id")]
            live = db.get_messages_as_conversation("s", include_row_ids=True)
            watermark = db.get_active_message_watermark("s")
            # Omit a held interior row to drive the named coverage-gap clone path.
            covered = [msg["_row_id"] for msg in live if msg != live[3]]
            carried = copy.deepcopy(live[-5:])
            compacted = [{"role": "user", "content": "summary", "_compressed_summary": True}, *carried]
            if mode == "no_plan":
                monkeypatch.setattr(db, "_display_fold_plan", lambda _sid: None)
            elif mode == "missing_index":
                db._write_sql("DROP INDEX idx_messages_display_identity")
            elif mode == "missing_backfill":
                db._write_sql("DROP INDEX idx_messages_display_backfill")
            if mode == "replacement":
                original_plan = db._display_fold_plan

                def replaced(sid):
                    plan = original_plan(sid)
                    with db._lock:
                        old = db._conn
                        db._conn = db._open_writer_conn()
                        old.close()
                    return plan

                monkeypatch.setattr(db, "_display_fold_plan", replaced)
            reconcile = db._reconcile_display_orders
            attempts = []
            if mode == "retry":
                def retry_after_fold(conn, sid, plan=None):
                    attempts.append(type(plan).__name__)
                    reconcile(conn, sid, plan)
                    if len(attempts) == 1:
                        # Exercise the real writer retry after inserts and display
                        # updates. total_changes survives the transaction rollback.
                        raise sqlite3.OperationalError("database is locked")

                monkeypatch.setattr(db, "_reconcile_display_orders", retry_after_fold)
            with monkeypatch.context() as local:
                if mode == "guard_unavailable":
                    import hermes_state_display_fold as fold
                    original_freshness = fold._freshness

                    def unavailable(conn):
                        if conn.in_transaction:
                            raise sqlite3.OperationalError("guard unavailable")
                        return original_freshness(conn)

                    local.setattr(fold, "_freshness", unavailable)
                local.setattr(db, "_WRITE_LOCK_WARN_S", 0.0)
                caplog.clear()
                db.archive_and_compact(
                    "s", compacted, watermark=watermark, tail_count=5,
                    carried_messages=[live[0]], covered_ids=covered if named else None)
            expected = "display_incremental=1" if mode == "incremental" else "display_fallback=1"
            assert any(expected in record.message for record in caplog.records)
            if mode == "retry":
                assert attempts == ["_AffectedFold", "_FoldPlan"]
                monkeypatch.setattr(db, "_reconcile_display_orders", reconcile)
            assert [tuple(row) for row in db._read_all("SELECT * FROM messages WHERE session_id = 'foreign'")] == foreign
            assert [tuple(row) for row in db._read_all(
                "SELECT id, display_identity, display_order FROM messages WHERE id BETWEEN 21 AND 24 ORDER BY id")] == untouched
            _assert_full_equivalent(db)
        finally:
            db.close()


def _interleaved_write(db, conn, mutation):
    schedules = {
        "append": [("INSERT INTO messages(session_id, role, content, timestamp) VALUES ('s', 'user', 'new', 900)", ())],
        "unrelated": [("INSERT INTO messages(session_id, role, content, timestamp) VALUES ('other', 'user', 'unrelated', 901)", ())],
        "payload": [("UPDATE messages SET content = 'changed' WHERE id = 2", ())],
        "non_null": [("UPDATE messages SET display_identity = zeroblob(32), display_order = 999 WHERE id = 2", ())],
        "visibility": [("UPDATE messages SET active = 1 WHERE id = 1", ())],
        "delete": [("DELETE FROM messages WHERE id = 2", ())],
        "metadata": [("UPDATE messages SET display_metadata = ? WHERE id = 2",
                      (json.dumps({MODEL_ONLY_DISPLAY_METADATA_KEY: True}),))],
        "rollback": [("SAVEPOINT change", ()), ("UPDATE messages SET display_order = 999 WHERE id = 2", ()),
                     ("ROLLBACK TO change", ()), ("RELEASE change", ())],
    }
    for sql, values in schedules[mutation]:
        conn.execute(sql, values)


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("mutation", ["append", "unrelated", "payload", "non_null", "visibility", "delete", "metadata", "rollback"])
def test_snapshot_interleaving_falls_back(tmp_path, monkeypatch, caplog, named, external, mutation):
    db = SessionDB(tmp_path / "interleaving.sqlite")
    try:
        db.create_session("s", source="test")
        db.create_session("other", source="test")
        db.append_messages_batch("s", [{"role": "assistant", "content": f"row {i}", "timestamp": 100 + i}
                                       for i in range(8)])
        db._write_sql("UPDATE messages SET active = 0, compacted = 0 WHERE id = 1")
        live = db.get_messages_as_conversation("s", include_row_ids=True)
        entered, release = threading.Event(), threading.Event()
        original_key = db._display_dedupe_key

        def pause(row):
            if threading.current_thread().name == "planner" and not entered.is_set():
                entered.set()
                assert release.wait(20)
            return original_key(row)

        monkeypatch.setattr(db, "_display_dedupe_key", pause)
        monkeypatch.setattr(db, "_WRITE_LOCK_WARN_S", 0.0)
        errors = []

        def compact():
            try:
                db.archive_and_compact(
                    "s", [{"role": "user", "content": "summary"}],
                    watermark=live[-1]["_row_id"],
                    covered_ids=[msg["_row_id"] for msg in live] if named else None)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=compact, name="planner")
        caplog.clear()
        worker.start()
        try:
            assert entered.wait(20)
            if external:
                with closing(sqlite3.connect(db.db_path)) as conn, conn:
                    _interleaved_write(db, conn, mutation)
            else:
                db._execute_write(lambda conn: _interleaved_write(db, conn, mutation))
        finally:
            release.set()
            worker.join(20)
        assert not worker.is_alive()
        assert not errors
        warning = next(record.message for record in caplog.records
                       if "archive_and_compact" in record.message and "held the write lock" in record.message)
        # Another connection's rolled-back transaction makes no visible change and
        # need not advance data_version. Our connection's total_changes does advance.
        if external and mutation == "rollback":
            assert "display_incremental=1" in warning or "display_fallback=1" in warning
        else:
            assert "display_fallback=1" in warning
        _assert_full_equivalent(db)
    finally:
        db.close()


def _bulk_history(db, size, group_size=3, nulls=0):
    db.create_session("s", source="test")
    rows = []
    for index in range(size):
        logical = index // group_size if index < 20 * group_size else index
        content, timestamp = f"history {logical}:" + "x" * 256, float(logical)
        identity = db._display_identity(("assistant", content, timestamp, None, None, None))
        order = logical * group_size + 1 if index < 20 * group_size else index + 1
        rows.append(("s", "assistant", content, timestamp, identity, order))

    def insert(conn):
        conn.executemany(
            "INSERT INTO messages(session_id, role, content, timestamp, display_identity, display_order, active, compacted) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 1)", rows)
        if nulls:
            conn.execute("UPDATE messages SET display_identity = NULL WHERE id > ?", (size - nulls,))

    db._execute_write(insert)
    return [{"role": "assistant", "content": f"history {i % 20}:" + "x" * 256, "timestamp": float(i % 20)}
            for i in range(40)]


def test_enumerated_work_is_bounded_by_affected_population(tmp_path, monkeypatch, caplog):
    counts, vm_work = [], []
    for size, group_size, nulls in [(30000, 3, 0), (60000, 3, 0), (100000, 3, 0), (9000, 9, 0), (9000, 3, 17), (9000, 3, 601)]:
        db = SessionDB(tmp_path / f"work-{size}-{group_size}-{nulls}.sqlite")
        try:
            compacted = _bulk_history(db, size, group_size, nulls)
            statements = []
            real_reconcile = db._reconcile_display_orders

            def traced(conn, sid, plan=None):
                ticks = [0]
                conn.set_trace_callback(statements.append)
                conn.set_progress_handler(lambda: ticks.__setitem__(0, ticks[0] + 100) or 0, 100)
                try:
                    return real_reconcile(conn, sid, plan)
                finally:
                    conn.set_progress_handler(None, 0)
                    conn.set_trace_callback(None)
                    vm_work.append(ticks[0])

            monkeypatch.setattr(db, "_reconcile_display_orders", traced)
            monkeypatch.setattr(db, "_WRITE_LOCK_WARN_S", 0.0)
            caplog.clear()
            db.archive_and_compact("s", compacted)
            warning = next(record.message for record in caplog.records if "held the write lock" in record.message)
            assert "display_incremental=1" in warning
            rows = dict((name, int(value)) for name, value in re.findall(r"(\w+)=(\d+)(?=,|\])", warning))
            counts.append(rows["display_materialized_rows"])
            assert rows["display_new_rows"] == 40
            scans = [sql for sql in statements if sql.startswith("SELECT") and "idx_messages_display_identity" in sql]
            assert scans and all("display_identity IN (" in sql for sql in scans)
            monkeypatch.setattr(db, "_reconcile_display_orders", real_reconcile)
            _assert_full_equivalent(db)
        finally:
            db.close()
    assert counts[:3] == [100, 100, 100]
    assert max(vm_work[:3]) <= min(vm_work[:3]) + 100
    assert counts[3:] == [220, 117, 701]
