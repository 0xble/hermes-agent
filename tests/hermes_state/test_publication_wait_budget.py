"""Writer patience includes the in-process mutex, not just SQLite's busy handler."""

import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

from hermes_state import SessionDB


def test_writer_patience_bounds_local_mutex_wait(tmp_path):
    db = SessionDB(tmp_path / "wait.db")
    held = threading.Event()
    release = threading.Event()

    def reader():
        with db._lock:
            held.set()
            release.wait(2)

    thread = threading.Thread(target=reader)
    thread.start()
    assert held.wait(2)
    timer = threading.Timer(0.4, release.set)
    timer.start()
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db._execute_write(lambda conn: conn.execute("SELECT 1"), patience_s=0.03)
        assert time.monotonic() - started < 0.3
    finally:
        release.set()
        thread.join(2)
        timer.cancel()
        db.close()


@pytest.mark.parametrize("operation", ["acquire", "refresh"])
def test_lease_ttl_starts_after_writer_wait(tmp_path, operation):
    db = SessionDB(tmp_path / "lease.db")
    try:
        db.create_session("s", "test")
        if operation == "refresh":
            assert db.try_acquire_compression_lock("s", "holder", ttl_seconds=10)
        execute = db._execute_write
        clock = [100.0]

        def delayed_write(fn, **kwargs):
            clock[0] += 100
            return execute(fn, **kwargs)

        with patch("hermes_state_compression.time.time", side_effect=lambda: clock[0]), \
                patch.object(db, "_execute_write", side_effect=delayed_write):
            operation_fn = db.refresh_compression_lock if operation == "refresh" else db.try_acquire_compression_lock
            assert operation_fn("s", "holder", ttl_seconds=10)
        row = db._read_one("SELECT expires_at FROM compression_locks WHERE session_id='s'")
        assert row[0] == 210.0
    finally:
        db.close()


def test_lost_compaction_lease_is_not_retried(tmp_path):
    from hermes_state_errors import SessionCompressionInProgressError

    db = SessionDB(tmp_path / "lost.db")
    try:
        db.create_session("s", "test")
        with patch.object(db, "_WRITE_PATIENCE_S", 0.02), patch("hermes_state.time.sleep") as sleep:
            with pytest.raises(SessionCompressionInProgressError):
                db.archive_and_compact("s", [{"role": "user", "content": "summary"}],
                                       lock_holder="lost")
        sleep.assert_not_called()
        assert db.get_messages("s") == []
    finally:
        db.close()
