"""Opening a healthy store survives schema invalidation during FTS construction."""

import sqlite3

import pytest

from hermes_state import SessionDB


@pytest.mark.parametrize("table", ["messages_fts", "messages_fts_trigram"])
def test_open_retries_fts_constructor_schema_race(tmp_path, monkeypatch, table):
    path = tmp_path / "state.db"
    seed = SessionDB(db_path=path)
    seed.close()
    peer = sqlite3.connect(path, isolation_level=None)
    original = SessionDB._fts_table_probe
    failures = []
    abandoned_connections = []
    churn = 0

    def invalidate_schema():
        nonlocal churn
        churn += 1
        peer.execute(f"CREATE TABLE schema_race_{churn}(value)")

    def race_once(self, cursor, table_name):
        if table_name != table or failures:
            return original(self, cursor, table_name)

        def authorizer(action, name, column, database, source):
            if action == sqlite3.SQLITE_READ and name == table + "_config" and column == "k":
                # Force the same legal interleaving on each internal SQLite reprepare:
                # a sibling commits DDL while this connection constructs the FTS table.
                invalidate_schema()
            return sqlite3.SQLITE_OK

        invalidate_schema()  # Expire any already-cached vtable on this connection.
        cursor.connection.set_authorizer(authorizer)
        try:
            return original(self, cursor, table_name)
        except sqlite3.OperationalError as exc:
            failures.append(exc)
            abandoned_connections.append(cursor.connection)
            assert exc.sqlite_errorcode == sqlite3.SQLITE_SCHEMA
            assert "vtable constructor failed" in str(exc)
            raise
        finally:
            cursor.connection.set_authorizer(None)

    monkeypatch.setattr(SessionDB, "_fts_table_probe", race_once)
    try:
        db = SessionDB(db_path=path)
        try:
            assert len(failures) == 1
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                abandoned_connections[0].execute("SELECT 1")
            assert db._fts_enabled and db._trigram_available
            db.create_session("claim", source="conformance")
            assert db.request_handoff("claim", "telegram")
            assert db.claim_handoff("claim")
            assert not db.claim_handoff("claim")
            assert db._conn is not None
            assert db._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            db.close()
    finally:
        peer.close()


@pytest.mark.parametrize("code", [None, sqlite3.SQLITE_ERROR, sqlite3.SQLITE_CORRUPT])
def test_open_does_not_retry_nontransient_constructor_failure(tmp_path, monkeypatch, code):
    error = sqlite3.OperationalError("vtable constructor failed: messages_fts")
    if code is not None:
        error.sqlite_errorcode = code
    attempts = []

    def broken_schema(self):
        attempts.append(self._conn)
        raise error

    monkeypatch.setattr(SessionDB, "_init_schema", broken_schema)
    with pytest.raises(sqlite3.OperationalError) as caught:
        SessionDB(db_path=tmp_path / "state.db")
    assert caught.value is error
    assert len(attempts) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        attempts[0].execute("SELECT 1")


def test_schema_retry_does_not_extend_startup_deadline(tmp_path, monkeypatch):
    error = sqlite3.OperationalError("vtable constructor failed: messages_fts")
    error.sqlite_errorcode = sqlite3.SQLITE_SCHEMA
    attempts = []

    def changing_schema(self):
        attempts.append(self._conn)
        raise error

    monkeypatch.setattr(SessionDB, "_init_schema", changing_schema)
    monkeypatch.setattr(SessionDB, "_WRITE_PATIENCE_S", 0.0)
    with pytest.raises(sqlite3.OperationalError) as caught:
        SessionDB(db_path=tmp_path / "state.db")
    assert caught.value is error
    assert len(attempts) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        attempts[0].execute("SELECT 1")
