"""Only compaction publication bounds the in-process mutex admission wait."""

import threading
from unittest.mock import patch

import pytest

from hermes_state import SessionDB


@pytest.mark.parametrize("operation", ["write", "degraded_read"])
def test_publication_mutex_timeout_attributes_holders(tmp_path, operation):
    from hermes_state_publication import LocalStateLockTimeout, publication_metrics

    db = SessionDB(tmp_path / "wait.db")
    held = threading.Event()
    release = threading.Event()

    def reader():
        with db._lock:
            held.set()
            release.wait(10)

    thread = threading.Thread(target=reader)
    thread.start()
    assert held.wait(10)
    try:
        db._wal_active = False
        with publication_metrics(0), patch("hermes_state_lockowners.log_write_lock_holders") as holders:
            with pytest.raises(LocalStateLockTimeout, match="local SessionDB connection mutex"):
                if operation == "write":
                    db._execute_write(lambda conn: conn.execute("SELECT 1"), patience_s=0)
                else:
                    db._read_one("SELECT 1")
        holders.assert_called_once_with(db.db_path, 0)
    finally:
        release.set()
        thread.join(10)
        db.close()


@pytest.mark.parametrize("operation", ["append_message", "set_session_title"])
def test_ordinary_writer_waits_for_mutex_without_dropping_data(tmp_path, operation):
    db = SessionDB(tmp_path / "ordinary.db")
    db.create_session("s", "test")
    lock = db._lock
    waiting = threading.Event()
    waits, errors = [], []

    class ObservedLock:
        def acquire(self, blocking=True, timeout=-1):
            waits.append(timeout)
            waiting.set()
            return lock.acquire(blocking, timeout)

        def release(self):
            lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_exc):
            self.release()

    def write():
        try:
            if operation == "append_message":
                db.append_message("s", "user", "must survive mutex contention")
            else:
                db.set_session_title("s", "must survive mutex contention")
        except Exception as exc:
            errors.append(exc)

    db._lock = ObservedLock()
    thread = threading.Thread(target=write)
    try:
        # An expired SQLite retry budget must not become an ordinary writer's
        # mutex timeout. Hold the real mutex until the writer queues behind it.
        with patch.object(db, "_TRANSCRIPT_WRITE_PATIENCE_S", 0), patch.object(db, "_WRITE_PATIENCE_S", 0):
            with lock:
                thread.start()
                assert waiting.wait(10)
            thread.join(10)
        assert not thread.is_alive()
        assert errors == []
        assert waits and all(timeout == -1 for timeout in waits)
        if operation == "append_message":
            assert [row["content"] for row in db.get_messages("s")] == ["must survive mutex contention"]
        else:
            assert db.get_session_title("s") == "must survive mutex contention"
    finally:
        thread.join(10)
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
