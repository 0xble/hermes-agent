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


@pytest.mark.parametrize("abort_final_attempt", [False, True])
def test_publication_and_attempt_metrics_keep_separate_retry_scopes(
    tmp_path, monkeypatch, abort_final_attempt
):
    import sqlite3

    from hermes_state_publication import publication_metrics, record_publication_write
    from hermes_state_write_metrics import WriteMetrics, current_write_metrics

    db = SessionDB(tmp_path / "metrics.db")
    outer = WriteMetrics()
    token = current_write_metrics.set(outer)
    try:
        db.create_session("s", "test")
        attempts, changes = [], []

        def publish(conn):
            attempts.append(current_write_metrics.get())
            before = conn.total_changes
            db._insert_message_rows(conn, "s", [{"role": "user", "content": "summary"}])
            changes.append(conn.total_changes - before)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
            if abort_final_attempt:
                raise ValueError("abort publication")

        monkeypatch.setattr(db, "_sleep_before_write_retry", lambda *_args: True)
        conn = db._conn
        assert conn is not None
        changes_before = conn.total_changes
        with publication_metrics(10) as publication:
            if abort_final_attempt:
                with pytest.raises(ValueError, match="abort publication"):
                    db._execute_write(publish)
            else:
                db._execute_write(publish)
            assert current_write_metrics.get() is outer
            # SQLite may defer additional FTS changes until commit, after the callback.
            assert publication["publish_rows_written"] == conn.total_changes - changes_before
            assert publication["publish_rows_written"] >= sum(changes)
            assert publication["publish_hold_ms"] >= 0
            assert publication["publish_wait_ms"] >= 0
        assert len(attempts) == 2
        assert attempts[0] is not attempts[1]
        for attempt in attempts:
            assert attempt.rows == {"inserted": 1}
            assert "callback_total" in attempt.phases
        assert "rollback" in attempts[0].phases
        assert ("rollback" if abort_final_attempt else "commit") in attempts[1].phases
        assert outer.rows == {} and outer.phases == {}
        counted_rows = publication["publish_rows_written"]
        record_publication_write(rows_written=999)
        assert publication["publish_rows_written"] == counted_rows
        assert len(db.get_messages("s")) == (0 if abort_final_attempt else 1)
    finally:
        current_write_metrics.reset(token)
        db.close()
