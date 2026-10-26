"""Writer-side repair must not hand the GIL off once per retained message.

The whole-index path already batches SQLite results. Repair/backfill and no-plan
paths must do so too, while producing the same fold and keeping payload pages small.
"""

from contextlib import closing

import pytest

from hermes_state import SessionDB


class _Cursor:
    def __init__(self, cursor, owner):
        self.cursor, self.owner = cursor, owner

    def __iter__(self):
        for row in self.cursor:
            self.owner.steps += 1
            yield row

    def fetchone(self):
        row = self.cursor.fetchone()
        self.owner.steps += row is not None
        return row

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class _Connection:
    def __init__(self, conn):
        self.conn, self.steps = conn, 0
        self.update_pages = []

    def execute(self, sql, params=()):
        if sql.startswith("UPDATE messages SET display_order = repairs."):
            self.update_pages.append(len(params) // 3)
        return _Cursor(self.conn.execute(sql, params), self)

    def executemany(self, sql, params):
        assert not sql.startswith("UPDATE messages SET display_order"), "writer repairs must batch UPDATEs"
        return self.conn.executemany(sql, params)

    def __getattr__(self, name):
        return getattr(self.conn, name)


@pytest.mark.parametrize("planned", [False, True])
def test_writer_backlog_uses_bounded_steps_and_matches_full_fold(tmp_path, planned):
    with closing(SessionDB(tmp_path / "fold.sqlite")) as db:
        db.create_session("s", source="test")
        db.append_messages_batch("s", [
            {"role": "assistant", "content": f"group {i % 30}", "timestamp": 1.7e9 + i % 30}
            for i in range(601)])
        # An old writer or payload invalidation can leave a large NULL backlog.
        db._write_sql("UPDATE messages SET display_identity=NULL, display_order=NULL WHERE session_id='s'")
        plan = db._display_fold_plan("s") if planned else None
        if planned:
            assert plan is not None
        rows = lambda: [tuple(r) for r in db._conn.execute(
            "SELECT id, display_identity, display_order FROM messages ORDER BY id")]
        db._conn.execute("BEGIN IMMEDIATE")
        try:
            selection = db._begin_display_fold(db._conn, plan)
            counting = _Connection(db._conn)
            db._reconcile_display_orders(counting, "s", selection)
            result = rows()
            steps = counting.steps
            assert sum(counting.update_pages) == 601
            assert max(counting.update_pages) <= 200
            # Full payload recomputation is the correctness oracle, not cached identities.
            db._conn.execute("UPDATE messages SET display_identity=NULL, display_order=NULL")
            db._reconcile_display_orders(db._conn, "s")
            assert rows() == result
            assert all(identity is not None and order <= row_id for row_id, identity, order in result)
            # Page-level SQLite round trips, independent of individual cursor rows.
            assert steps < 30, f"{steps} SQLite result rows crossed the writer's GIL boundary"
        finally:
            db._conn.rollback()


@pytest.mark.parametrize("named", [False, True])
def test_archive_payload_repair_pages_stay_bounded(tmp_path, monkeypatch, named):
    import hermes_state_display_fold as folding

    with closing(SessionDB(tmp_path / "archive.sqlite")) as db:
        db.create_session("s", source="test")
        db.append_messages_batch("s", [
            {"role": "assistant", "content": f"message {i}", "timestamp": 1.7e9 + i}
            for i in range(421)])
        db._write_sql("UPDATE messages SET active=0, compacted=1 WHERE id <= 401")
        live = db.get_messages_as_conversation("s", include_row_ids=True)
        db._write_sql("UPDATE messages SET display_identity=NULL, display_order=NULL WHERE session_id='s'")
        original = folding.fold_rows
        pages = []

        def bounded(conn, sql, params, *columns):
            result = original(conn, sql, params, *columns)
            pages.append(len(result))
            assert len(result) <= 200
            return result

        monkeypatch.setattr(folding, "fold_rows", bounded)
        count = db.archive_and_compact("s", [
            {"role": "assistant", "content": "summary", "timestamp": 1.8e9, "_compressed_summary": True},
            *live[-2:]], watermark=421, tail_count=2,
            covered_ids=[row["_row_id"] for row in live] if named else None)
        assert count == 3
        assert pages and max(pages) == 200
        result = [tuple(r) for r in db._conn.execute(
            "SELECT id, display_identity, display_order FROM messages ORDER BY id")]
        db._execute_write(lambda conn: db._reconcile_display_orders(conn, "s"))
        assert [tuple(r) for r in db._conn.execute(
            "SELECT id, display_identity, display_order FROM messages ORDER BY id")] == result
