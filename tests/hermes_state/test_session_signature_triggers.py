"""Session projection invalidation adds no journal writes for unrelated updates."""

import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_session_signature import (
    SESSION_SIGNATURE_FIELDS, SESSION_SIGNATURE_TABLE_SQL, SESSION_SIGNATURE_TRIGGER_SQL,
)


@pytest.mark.parametrize("legacy_collation", [False, True])
def test_projection_journals_only_changed_signature_fields(tmp_path, legacy_collation):
    path = tmp_path / "state.db"
    if legacy_collation:
        with sqlite3.connect(path) as conn:
            fields = ["id TEXT PRIMARY KEY", "title COLLATE NOCASE"]
            fields += [field for field in SESSION_SIGNATURE_FIELDS if field not in ("id", "title")]
            fields += ["last_activity_at", "last_activity_description", "transcript_revision INTEGER DEFAULT 0"]
            conn.execute(f"CREATE TABLE sessions ({', '.join(fields)})")
            conn.executescript(SESSION_SIGNATURE_TABLE_SQL + SESSION_SIGNATURE_TRIGGER_SQL)
            conn.execute("INSERT INTO sessions(id) VALUES ('s')")
    else:
        with SessionDB(path) as db:
            db.create_session("s", source="cli")
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM session_projection_changes")
        for sql in (
            "UPDATE sessions SET last_activity_at = 200",
            "UPDATE sessions SET last_activity_description = 'working'",
            "UPDATE sessions SET transcript_revision = transcript_revision + 1",
            "UPDATE sessions SET title = title",
            "UPDATE sessions SET rowid = rowid",
            "UPDATE sessions SET _rowid_ = _rowid_",
            "UPDATE sessions SET oid = oid",
        ):
            before = conn.total_changes
            conn.execute(sql)
            assert conn.total_changes - before == 1, sql
            assert conn.execute("SELECT COUNT(*) FROM session_projection_changes").fetchone()[0] == 0
        conn.execute("UPDATE sessions SET title = 'changed'")
        assert conn.execute("SELECT COUNT(*) FROM session_projection_changes").fetchone()[0] == 1
        conn.execute("DELETE FROM session_projection_changes")
        old_rowid = conn.execute("SELECT rowid FROM sessions").fetchone()[0]
        conn.execute("UPDATE sessions SET rowid = 99")
        assert conn.execute("SELECT session_rowid FROM session_projection_changes ORDER BY sequence").fetchall() == [
            (old_rowid,), (99,),
        ]
        conn.execute("DELETE FROM session_projection_changes")
        conn.execute("UPDATE sessions SET rowid = 100, title = 'moved and renamed'")
        assert conn.execute("SELECT session_rowid FROM session_projection_changes ORDER BY sequence").fetchall() == [
            (99,), (100,),
        ]
        for alias, rowid in (("_rowid_", 101), ("oid", 102)):
            conn.execute("DELETE FROM session_projection_changes")
            conn.execute(f"UPDATE sessions SET {alias} = ?", (rowid,))
            assert conn.execute("SELECT session_rowid FROM session_projection_changes ORDER BY sequence").fetchall() == [
                (rowid - 1,), (rowid,),
            ]
        for before, after in (("case", "CASE"), (1, 1.0)):
            conn.execute("UPDATE sessions SET title = ?", (before,))
            conn.execute("DELETE FROM session_projection_changes")
            conn.execute("UPDATE sessions SET title = ?", (after,))
            assert conn.execute("SELECT COUNT(*) FROM session_projection_changes").fetchone()[0] == 1


def test_settle_replaces_broad_projection_trigger_and_then_stays_read_only(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("s", source="cli")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER sessions_projection_update")
        conn.execute("CREATE TRIGGER sessions_projection_update AFTER UPDATE ON sessions BEGIN "
                     "INSERT INTO session_projection_changes(session_rowid) VALUES (new.rowid); END")
    with SessionDB(path):
        pass
    with sqlite3.connect(path) as conn:
        before = conn.execute("SELECT MAX(sequence) FROM session_projection_changes").fetchone()[0]
        conn.execute("UPDATE sessions SET last_activity_at = 200")
        assert conn.execute("SELECT MAX(sequence) FROM session_projection_changes").fetchone()[0] == before
        conn.commit()
        schema = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute("BEGIN IMMEDIATE")
        with sqlite3.connect(path, timeout=0.05) as reader:
            SessionDB._execute_ddl_skipping_settled_triggers(reader.cursor(), SESSION_SIGNATURE_TRIGGER_SQL)
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == schema


@pytest.mark.parametrize("opener", ["startup", "reconcile"])
@pytest.mark.parametrize("stale_trigger", [False, True])
def test_reconcile_restores_projection_trigger_after_adding_columns(tmp_path, opener, stale_trigger):
    from hermes_state_schema import apply_declared_schema, reconcile_state_schema

    path = tmp_path / "state.db"
    missing = ("archived", "pinned", "last_read_at")
    with SessionDB(path) as db:
        db.create_session("kept", source="cli")
        db.append_message("kept", role="user", content="history survives")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER sessions_projection_update")
        for field in missing:
            conn.execute(f"ALTER TABLE sessions DROP COLUMN {field}")
        # SQLite accepts CREATE TRIGGER with unresolved old/new columns. An earlier
        # schema settle may have installed it on a store that is still behind.
        if stale_trigger:
            conn.executescript(SESSION_SIGNATURE_TRIGGER_SQL)
            with pytest.raises(sqlite3.OperationalError, match="no such column: old.archived"):
                conn.execute("UPDATE sessions SET title = 'blocked'")
        else:
            conn.commit()
        if opener == "reconcile":
            conn.execute("BEGIN")
            apply_declared_schema(conn.cursor())
            conn.rollback()
            assert not set(missing) & {row[1] for row in conn.execute("PRAGMA table_xinfo(sessions)")}
            if stale_trigger:
                with pytest.raises(sqlite3.OperationalError, match="no such column: old.archived"):
                    conn.execute("UPDATE sessions SET title = 'still blocked'")
            reconcile_state_schema(conn)
    if opener == "startup":
        with SessionDB(path):
            pass
    with sqlite3.connect(path) as conn:
        assert set(missing) <= {row[1] for row in conn.execute("PRAGMA table_xinfo(sessions)")}
        assert conn.execute("SELECT content FROM messages").fetchall() == [("history survives",)]
        conn.execute("DELETE FROM session_projection_changes")
        conn.execute("UPDATE sessions SET last_activity_at = 200, title = title")
        assert conn.execute("SELECT COUNT(*) FROM session_projection_changes").fetchone()[0] == 0
        for field in missing:
            conn.execute(f"UPDATE sessions SET {field} = 1")
        assert conn.execute("SELECT COUNT(*) FROM session_projection_changes").fetchone()[0] == len(missing)
