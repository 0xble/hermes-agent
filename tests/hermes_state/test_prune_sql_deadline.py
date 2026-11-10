"""Retention deadlines cover pending cleanup and one expensive FTS-trigger SQL step."""
from contextlib import closing
from datetime import datetime
import time
import sqlite3
import threading

import pytest

from hermes_state import SessionDB
from hermes_state_mutation_retirement import RETIRED_PREFIX, retire_prunable
from hermes_state_deletion import stage_delete
from tests.hermes_state.test_prune_pass_bounds import _old_ended


def _slow_delete(db):
    # Real SQLite VM work inside the messages DELETE, as FTS delete triggers perform.
    # No sleep/UDF: the progress handler must interrupt the actual statement.
    db._execute_write(lambda conn: conn.execute("""
        CREATE TRIGGER expensive_delete BEFORE DELETE ON messages BEGIN
          SELECT (WITH RECURSIVE work(n) AS
            (SELECT 1 UNION ALL SELECT n+1 FROM work WHERE n < 5000000)
            SELECT sum(n) FROM work);
        END
    """))


@pytest.mark.parametrize("pending", [False, True])
def test_window_end_interrupts_inflight_cleanup_and_next_pass_resumes(tmp_path, monkeypatch, pending):
    import hermes_time
    import hermes_cli.maintenance_window as window
    # A real 05:00-06:00 window ending shortly, no --now or user configuration change.
    monkeypatch.setattr(window, "deletion_window_spec", lambda: "05:00-06:00")
    monkeypatch.setattr(hermes_time, "now", lambda: datetime(2026, 11, 10, 5, 59, 59, 800000))
    with closing(SessionDB(tmp_path / "state.db")) as db:
        _old_ended(db, "old")
        if pending:
            def stage(conn):
                retire_prunable(conn, ["old"], defer_messages=True)
                stage_delete(conn, ["old"])
            db._execute_write(stage)
        _slow_delete(db)
        deleted_ids = []
        started = time.monotonic()
        deleted = db.prune_sessions(older_than_days=90, within_deletion_window=True, deleted_ids=deleted_ids)
        assert time.monotonic() - started < 2
        # Interrupted page rolls back, but the committed visibility fence survives.
        assert db._read_one("SELECT count(*) FROM messages WHERE session_id='old'")[0] == 1
        assert db.get_meta(RETIRED_PREFIX + "old") is not None
        assert deleted == (0 if pending else 1)
        assert deleted_ids == ([] if pending else ["old"])
        # Ordinary writes are not contaminated by this pass's progress handler/busy timeout.
        db.set_meta("foreground", "ok")
        db._execute_write(lambda conn: conn.execute("DROP TRIGGER expensive_delete"))
        monkeypatch.setattr(hermes_time, "now", lambda: datetime(2026, 11, 11, 5, 1))
        assert db.prune_sessions(older_than_days=90, within_deletion_window=True) == 0
        assert db._read_one("SELECT count(*) FROM messages WHERE session_id='old'")[0] == 0
        assert db._read_one("SELECT count(*) FROM sessions WHERE id='old'")[0] == 0
        assert db._read_one("PRAGMA integrity_check")[0] == "ok"


def test_runtime_cap_covers_candidate_sql_and_preserves_uncommitted_sessions(tmp_path, monkeypatch):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        _old_ended(db, "old")
        # Candidate selection itself can be expensive, before the first batch check.
        real_read = db._read_all
        def read(sql, params=()):
            if "SELECT s.id FROM sessions s WHERE" in sql:
                return real_read("WITH RECURSIVE work(n) AS (SELECT 1 UNION ALL "
                                 "SELECT n+1 FROM work WHERE n < 5000000) SELECT n FROM work")
            return real_read(sql, params)
        monkeypatch.setattr(db, "_read_all", read)
        started = time.monotonic()
        assert db.prune_sessions(older_than_days=90, max_seconds=0.05) == 0
        assert time.monotonic() - started < 2
        assert db.get_session("old") is not None


def test_one_expensive_statement_releases_writer_without_waiting_for_pass_end(tmp_path):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        _old_ended(db, "old")
        _slow_delete(db)
        started = time.monotonic()
        assert db.prune_sessions(older_than_days=90, max_seconds=30) == 1
        assert time.monotonic() - started < 2
        assert db._read_one("SELECT count(*) FROM messages WHERE session_id='old'")[0] == 1
        db.set_meta("foreground", "ok")


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "10"])
def test_invalid_runtime_cap_rejected_without_selection(tmp_path, value):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        _old_ended(db, "old")
        with pytest.raises(ValueError, match="finite positive"):
            db.prune_sessions(max_seconds=value)
        assert db.get_session("old") is not None


@pytest.mark.parametrize("local", [False, True])
def test_prune_yields_to_foreground_writer_instead_of_waiting_for_it(tmp_path, local):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        _old_ended(db, "old")
        held, release = threading.Event(), threading.Event()
        def foreground():
            if local:
                with db._lock:
                    held.set()
                    release.wait(5)
            else:
                conn = sqlite3.connect(db.db_path)
                conn.execute("BEGIN IMMEDIATE")
                held.set()
                release.wait(5)
                conn.rollback()
                conn.close()
        thread = threading.Thread(target=foreground)
        thread.start()
        assert held.wait(2)
        try:
            started = time.monotonic()
            assert db.prune_sessions(older_than_days=90, max_seconds=0.2) == 0
            assert time.monotonic() - started < 1
        finally:
            release.set()
            thread.join()
        assert db.get_session("old") is not None
