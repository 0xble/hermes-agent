"""The gateway event loop must not wait on locks that state.db writers hold.

Both waits below parked the live loop for seconds at a time (py-spy: 45% of loop time), which
delayed Telegram delivery and surfaced as spurious connect timeouts.
"""
from __future__ import annotations

from contextlib import closing
import shutil
import sqlite3
import threading
import time

import pytest

import hermes_loop_guard
from hermes_state import SessionDB
from tools import async_delegation


def _as_loop_thread(fn):
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    try:
        return fn()
    finally:
        hermes_loop_guard.set_loop_thread(None)


def test_get_session_does_not_drain_a_busy_token_writer_on_the_loop_thread(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db._token_writer_busy = True  # a live writer mid-commit behind the state.db write lock
        started = time.monotonic()
        _as_loop_thread(lambda: db.get_session("absent"))
        assert time.monotonic() - started < 1.0
    finally:
        db._token_writer_busy = False
        db.close()


def test_token_flush_keeps_its_ordering_wait_everywhere(tmp_path):
    # Writers use the flush as an ordering barrier, so it must still wait even on the loop thread.
    db = SessionDB(tmp_path / "state.db")
    try:
        db._token_writer_busy = True
        started = time.monotonic()
        assert _as_loop_thread(lambda: db.flush_token_counts(timeout=0.3)) is False
        assert time.monotonic() - started >= 0.25
    finally:
        db._token_writer_busy = False
        db.close()


def test_active_count_does_not_queue_behind_a_records_lock_holder():
    held, release = threading.Event(), threading.Event()

    def holder():
        with async_delegation._records_lock:  # e.g. a state.db commit under the registry lock
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(2)
    try:
        started = time.monotonic()
        async_delegation.active_count()
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        thread.join(2)


def test_delegation_ledger_reconciles_schema_once_per_database_file(tmp_path, monkeypatch):
    import hermes_state_schema
    from tools import async_delegation_ledger

    calls = []
    real = hermes_state_schema.reconcile_state_schema
    monkeypatch.setattr(hermes_state_schema, "reconcile_state_schema", lambda conn: (calls.append(1), real(conn)))
    monkeypatch.setattr(async_delegation, "_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(async_delegation_ledger, "_SCHEMA_RECONCILED", {})
    for _ in range(3):
        with async_delegation._transaction() as conn:
            conn.execute("SELECT count(*) FROM async_delegations").fetchone()
    assert len(calls) == 1
    # A replaced state.db (restore, recovery) is a new file and must be reconciled again.
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"state.db{suffix}").unlink(missing_ok=True)
    with async_delegation._transaction() as conn:
        conn.execute("SELECT count(*) FROM async_delegations").fetchone()
    assert len(calls) == 2


@pytest.mark.parametrize("reuse_inode", [False, True], ids=["new-file", "same-inode"])
def test_delegation_ledger_reconciles_same_cookie_older_replacement(tmp_path, monkeypatch, reuse_inode):
    import hermes_state_schema

    calls = []
    real = hermes_state_schema.reconcile_state_schema
    monkeypatch.setattr(hermes_state_schema, "reconcile_state_schema", lambda conn: (calls.append(1), real(conn)))
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(async_delegation, "_db_path", lambda: db_path)
    from tools import async_delegation_ledger
    monkeypatch.setattr(async_delegation_ledger, "_SCHEMA_RECONCILED", {})

    with async_delegation._transaction() as conn:
        conn.execute(
            "INSERT INTO async_delegations "
            "(delegation_id, origin_session, state, dispatched_at, updated_at) "
            "VALUES ('original', 'session', 'completed', 1, 1)"
        )
        schema_cookie = conn.execute("PRAGMA schema_version").fetchone()[0]
    assert calls == [1]

    older_path = tmp_path / "older.db"
    with closing(sqlite3.connect(older_path)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(
            "CREATE TABLE async_delegations ("
            "delegation_id TEXT PRIMARY KEY, origin_session TEXT NOT NULL, "
            "state TEXT NOT NULL, dispatched_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO async_delegations "
            "(delegation_id, origin_session, state, dispatched_at, updated_at) "
            "VALUES ('restored', 'session', 'completed', 2, 2)"
        )
        conn.execute(f"PRAGMA schema_version = {schema_cookie}")
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == schema_cookie
        conn.commit()

    original_inode = db_path.stat().st_ino
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"state.db{suffix}").unlink(missing_ok=True)
    if reuse_inode:
        # A restore can overwrite the same inode; Linux can also reuse a deleted inode immediately.
        shutil.copyfile(older_path, db_path)
        assert db_path.stat().st_ino == original_inode
    else:
        older_path.replace(db_path)
        assert db_path.stat().st_ino != original_inode
    for _ in range(2):
        with async_delegation._transaction() as conn:
            assert conn.execute(
                "SELECT retry_root_started_at FROM async_delegations WHERE delegation_id = 'restored'"
            ).fetchone() == (None,)
    assert calls == [1, 1]
