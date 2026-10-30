"""Deletion must release the writer between bounded message/FTS batches."""

import sqlite3

import pytest

from hermes_state import SessionDB


@pytest.mark.parametrize("operation", ["single", "chain", "prune"])
def test_large_delete_bounds_each_transaction(tmp_path, operation):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("large", source="cli")
        db.append_messages_batch("large", [
            {"role": "user", "content": f"deletebatchneedle {i} 世界の記録 " + "payload " * 40}
            for i in range(1025)
        ])
        db.end_session("large", "done")
        db._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET started_at=1, ended_at=2, last_activity_at=2 WHERE id='large'"))
        deleted_per_transaction = []
        count = 0
        writer = sqlite3.connect(tmp_path / "state.db", timeout=1)

        def deleted():
            nonlocal count
            count += 1

        db._conn.create_function("record_deleted_message", 0, deleted)
        db._conn.execute("CREATE TEMP TRIGGER count_deleted AFTER DELETE ON messages "
                         "BEGIN SELECT record_deleted_message(); END")
        execute_write = db._execute_write

        def measured(callback, *args, **kwargs):
            nonlocal count
            count = 0
            result = execute_write(callback, *args, **kwargs)
            deleted_per_transaction.append(count)
            if count:
                # A separate gateway-style connection can commit between GC pages.
                writer.execute("INSERT OR REPLACE INTO state_meta(key,value) VALUES('writer-progress','ok')")
                writer.commit()
                assert db.get_session("large") is None
                assert db.search_messages("deletebatchneedle") == []
            return result

        db._execute_write = measured
        if operation == "single":
            assert db.delete_session("large")
        elif operation == "chain":
            assert db.delete_sessions(["large"], include_compression_chain=True) == 1
        else:
            assert db.prune_sessions(older_than_days=1) == 1
        writer.close()
        assert sum(deleted_per_transaction) == 1025
        assert max(deleted_per_transaction) <= 64, deleted_per_transaction
        assert db.get_session("large") is None
        assert db.search_messages("deletebatchneedle") == []
        assert db._read_one("SELECT COUNT(*) FROM messages")[0] == 0
        for table in ("messages_fts", "messages_fts_trigram", "messages_fts_cjk"):
            if db._read_one("SELECT 1 FROM sqlite_master WHERE name=?", (table,)):
                db._execute_write(lambda conn, table=table: conn.execute(
                    f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)"))
    finally:
        db.close()


@pytest.mark.parametrize("phase", ["retirement", "batch"])
def test_interrupted_delete_is_invisible_and_next_prune_resumes(tmp_path, monkeypatch, phase):
    from hermes_state_deletion import finish_pending_deletes
    from hermes_state_timeline import get_session_timeline

    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("doomed", source="cli")
    db.create_session("keep", source="cli")
    db._execute_write(lambda conn: conn.execute("UPDATE sessions SET title='Doomed', session_key='doomed-key' WHERE id='doomed'"))
    db.append_messages_batch("doomed", [
        {"role": "user", "content": f"pendingdeletionneedle {i}"} for i in range(200)
    ])
    execute_write = db._execute_write
    chunks = 0

    def interrupt(callback, *args, **kwargs):
        nonlocal chunks
        before = db._read_one("SELECT COUNT(*) FROM messages WHERE session_id='doomed'")[0]
        result = execute_write(callback, *args, **kwargs)
        after = db._read_one("SELECT COUNT(*) FROM messages WHERE session_id='doomed'")[0]
        pending = db._read_one("SELECT 1 FROM state_meta WHERE json_valid(value) "
                               "AND json_type(value, '$.cleanup')='object'")
        if chunks == 0 and ((phase == "batch" and after < before) or (phase == "retirement" and pending)):
            chunks += 1
            raise RuntimeError("simulated process interruption after committed chunk")
        return result

    monkeypatch.setattr(db, "_execute_write", interrupt)
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        db.delete_session("doomed")
    assert chunks == 1
    remaining = db._read_one("SELECT COUNT(*) FROM messages WHERE session_id='doomed'")[0]
    assert 0 < remaining <= 200
    assert remaining == 200 if phase == "retirement" else remaining < 200
    assert db.get_session("doomed") is None
    assert db.get_session_list_row("doomed") is None
    assert db.get_session_by_title("Doomed") is None
    assert db.get_session_title("doomed") is None
    assert db.resolve_session_by_title("Doomed") is None
    assert get_session_timeline(db, "doomed")["entries"] == []
    assert db.list_gateway_sessions(active_only=False) == []
    assert db.get_messages("doomed", include_inactive=True) == []
    assert db.get_messages("doomed", include_compacted=True) == []
    assert db.display_message_count("doomed") == 0
    assert db.search_messages("pendingdeletionneedle") == []
    with monkeypatch.context() as fallback:
        fallback.setattr(db, "_fts_stale", True)
        assert db.search_messages("pendingdeletionneedle") == []
        def broken_read(*args, **kwargs):
            raise sqlite3.DatabaseError("injected read failure")
        fallback.setattr(db, "_read_all", broken_read)
        with pytest.raises(sqlite3.DatabaseError, match="injected read failure"):
            db.search_messages("pendingdeletionneedle")
    assert all(row["id"] != "doomed" for row in db.list_sessions_rich(include_hidden=True, include_archived=True))
    with pytest.raises(sqlite3.IntegrityError):
        db.append_message("doomed", "user", "must not resurrect")
    survivor = db._read_one("SELECT id FROM messages WHERE session_id='doomed' LIMIT 1")[0]
    assert db.set_user_message_content("doomed", survivor, "changed after retirement") == 0
    with pytest.raises(sqlite3.IntegrityError):
        db.create_session("doomed", source="cli")
    with pytest.raises(sqlite3.IntegrityError):
        db.create_session("late-child", source="cli", parent_session_id="doomed")
    from hermes_state_mutation_retirement import RETIRED_PREFIX, prune_retired_tombstones
    execute_write(lambda conn: conn.execute(
        "UPDATE state_meta SET value=json_set(value, '$.expires', json('true'), '$.retired_at', 1) WHERE key=?",
        (RETIRED_PREFIX + "doomed",)))
    execute_write(lambda conn: prune_retired_tombstones(conn))
    assert db._read_one("SELECT 1 FROM state_meta WHERE key=?", (RETIRED_PREFIX + "doomed",))
    db.close()

    reopened = SessionDB(path)
    try:
        assert reopened.get_session("doomed") is None
        assert reopened.prune_sessions(older_than_days=1) == 0
        finish_pending_deletes(reopened)  # idempotent, also safe after another cleaner won
        assert reopened._read_one("SELECT 1 FROM sessions WHERE id='doomed'") is None
        assert reopened._read_one("SELECT 1 FROM messages WHERE session_id='doomed'") is None
        assert reopened.get_session("keep") is not None
        assert reopened._read_one("PRAGMA foreign_key_check") is None
        reopened._execute_write(lambda conn: conn.execute(
            "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"))
    finally:
        reopened.close()


def test_tool_payload_bytes_bound_each_transaction(tmp_path):
    from hermes_state_deletion import DELETE_BYTES

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("tools", source="cli")
        db.append_messages_batch("tools", [
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": str(i), "type": "function", "function": {"name": "payload", "arguments": "x" * 120000},
            }]} for i in range(6)
        ])
        sizes = dict(db._read_all("SELECT id,length(CAST(tool_calls AS BLOB)) FROM messages"))
        deleted = []
        pages = []
        db._conn.create_function("record_deleted_payload", 1, deleted.append)
        db._conn.execute("CREATE TEMP TRIGGER count_payload AFTER DELETE ON messages "
                         "BEGIN SELECT record_deleted_payload(old.id); END")
        execute_write = db._execute_write

        def measured(callback, *args, **kwargs):
            deleted.clear()
            result = execute_write(callback, *args, **kwargs)
            if deleted:
                pages.append(sum(sizes[row_id] for row_id in deleted))
            return result

        db._execute_write = measured
        assert db.delete_session("tools")
        assert len(pages) >= 3
        assert max(pages) <= DELETE_BYTES
        assert sum(pages) == sum(sizes.values())
    finally:
        db.close()


def test_chain_snapshot_fences_are_checked_before_retirement(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("root", source="cli")
        db.end_session("root", "compression")
        db.create_session("tip", source="cli", parent_session_id="root")
        assert not db.delete_session("tip", include_compression_chain=True, expected_delete_ids=["tip"])
        assert db.get_session("root") is not None
        assert db.get_session("tip") is not None
        assert db.delete_session("tip", include_compression_chain=True,
                                 expected_delete_ids=["root", "tip"], expected_display_messages={"root": [], "tip": []})
        assert db.get_session("root") is None
        assert db.get_session("tip") is None
    finally:
        db.close()


def test_failed_page_rolls_back_and_a_later_delete_recovers(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("failed", source="cli")
        db.append_messages_batch("failed", [{"role": "user", "content": "rollbackneedle"}] * 70)
        db._conn.execute("CREATE TEMP TRIGGER refuse_delete BEFORE DELETE ON messages "
                         "BEGIN SELECT RAISE(ABORT, 'injected page failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="injected page failure"):
            db.delete_session("failed")
        assert db.get_session("failed") is None
        assert db._read_one("SELECT COUNT(*) FROM messages WHERE session_id='failed'")[0] == 70
        assert db.search_messages("rollbackneedle") == []
        db._conn.execute("DROP TRIGGER refuse_delete")
        assert not db.delete_session("missing")  # entrypoint resumes the failed page first
        assert db._read_one("SELECT 1 FROM messages WHERE session_id='failed'") is None
        db._execute_write(lambda conn: conn.execute(
            "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"))
    finally:
        db.close()
