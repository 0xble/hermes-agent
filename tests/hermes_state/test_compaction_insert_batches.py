"""Compaction publication must bound insert steps without deferring any search index."""

import copy
import sqlite3

import pytest

from agent.message_metadata import DB_ROW_SNAPSHOT
from agent.transcript_repair import transcript_row_snapshot
from hermes_state import SessionDB
from tests.hermes_state.test_fts_cjk_bigram import cjk_so


class _InsertObserver:
    def __init__(self, conn):
        self.conn = conn
        self.pages = []

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, params=()):
        if sql.lstrip().startswith("INSERT INTO messages ("):
            self.pages.append(tuple(params))
        return self.conn.execute(sql, params)


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("variables", [999, 32766])
@pytest.mark.parametrize("native_cjk", [False, True])
def test_compaction_insert_steps_are_bounded_and_search_is_atomic(
    tmp_path, monkeypatch, request, named, variables, native_cjk,
):
    if native_cjk:
        monkeypatch.setenv("HERMES_FTS5_CJK_SO", str(request.getfixturevalue("cjk_so")))
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("topic", source="test")
        db.append_messages_batch("topic", [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"originaltoken {i}",
             "timestamp": 1700000000 + i} for i in range(202)])
        history = db.get_messages_as_conversation("topic", include_row_ids=True)
        # Do not infer fresh ids from MAX(id): AUTOINCREMENT may have a gap.
        db._write_sql("UPDATE sqlite_sequence SET seq = seq + 1000 WHERE name = 'messages'")
        old_limit = db._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, variables)
        original_insert = db._insert_message_rows
        observed = []

        def observe(conn, sid, messages, **kwargs):
            proxy = _InsertObserver(conn)
            result = original_insert(proxy, sid, messages, **kwargs)
            observed.extend(proxy.pages)
            return result

        monkeypatch.setattr(db, "_insert_message_rows", observe)
        summary = {"role": "user", "content": "newsummarytoken 日本語検索 " + "x" * (1024 * 1024),
                   "_compressed_summary": True, "timestamp": 1800000000}
        compacted = [summary, *copy.deepcopy(history[-200:])]
        kwargs = {"covered_ids": [msg["_row_id"] for msg in history]} if named else {}
        db.archive_and_compact("topic", compacted, watermark=history[-1]["_row_id"], tail_count=200, **kwargs)
        db._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit)
        rows = db._read_all("SELECT * FROM messages WHERE active = 1 ORDER BY id")
        assert [row["content"] for row in rows] == [msg["content"] for msg in compacted]
        for row, msg in zip(rows, compacted):
            assert msg["_row_id"] == row["id"] > history[-1]["_row_id"] + 1000
            assert msg[DB_ROW_SNAPSHOT] == transcript_row_snapshot(row)
        tables = ("messages_fts", "messages_fts_trigram", *(("messages_fts_cjk",) if native_cjk else ()))
        for table in tables:
            assert db._read_one(f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH 'newsummarytoken'")[0] == 1
            db._write_sql(f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)")
        assert db.search_messages("newsummarytoken")
        # Observe Python/SQLite calls, not trace callbacks (triggers repeat traces).
        assert len(observed) <= 8, f"{len(observed)} Python/SQLite INSERT steps for 201 compacted rows"
        assert all(len(params) <= variables for params in observed)
        for params in observed:
            rows_in_page = len(params) // 27
            size = sum(len(v.encode("utf-8")) if isinstance(v, str) else len(v)
                       if isinstance(v, bytes) else 8 for v in params if v is not None)
            assert rows_in_page <= 200
            assert size <= 1024 * 1024 or rows_in_page == 1

        before = [tuple(row) for row in db._read_all("SELECT * FROM messages ORDER BY id")]
        failed = [{"role": "user", "content": "rollbacksummarytoken", "_compressed_summary": True},
                  {"role": "assistant", "content": "rejecttoken"}]
        db._write_sql("CREATE TRIGGER reject_insert BEFORE INSERT ON messages WHEN new.content = 'rejecttoken' "
                      "BEGIN SELECT RAISE(ABORT, 'reject batch'); END")
        with pytest.raises(sqlite3.IntegrityError, match="reject batch"):
            db.archive_and_compact("topic", failed)
        assert [tuple(row) for row in db._read_all("SELECT * FROM messages ORDER BY id")] == before
        assert all("_row_id" not in msg and DB_ROW_SNAPSHOT not in msg and "timestamp" not in msg for msg in failed)
        assert db.search_messages("rollbacksummarytoken") == []
        for table in tables:
            db._write_sql(f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)")
    finally:
        db.close()
