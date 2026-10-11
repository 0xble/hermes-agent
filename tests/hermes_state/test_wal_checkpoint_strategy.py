"""Tests for SessionDB WAL checkpoint strategy (issue #45383).

Verifies that ALL checkpoints on the shared state.db use PASSIVE mode:
periodic, close(), and pre-VACUUM. TRUNCATE fires a full WAL reset, and
transient per-cron-run connections closing many times an hour would race
the live gateway writer and corrupt B-tree pages (#45383).
"""

import logging
import sqlite3
from unittest.mock import MagicMock

import pytest

from hermes_state import SessionDB


class TrackingConnection:
    """sqlite3.Connection proxy that records executed SQL strings."""

    def __init__(self, conn):
        self._conn = conn
        self.execute_calls = []

    def execute(self, sql, *args, **kwargs):
        self.execute_calls.append(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture()
def db(tmp_path):
    """Create a SessionDB with a temp database file."""
    db_path = tmp_path / "test_state.db"
    session_db = SessionDB(db_path=db_path)
    yield session_db
    try:
        session_db.close()
    except Exception:
        pass


class TestTryWalCheckpointPassive:
    """_try_wal_checkpoint() should use PASSIVE mode for periodic use."""

    def test_checkpoint_uses_passive_mode(self, db, monkeypatch):
        """PASSIVE checkpoint does not require exclusive lock — safe for large DBs."""
        tracked = []
        open_conn = db._open_writer_conn

        def tracking_open():
            tracked.append(TrackingConnection(open_conn()))
            return tracked[-1]

        # The periodic checkpoint runs on its own connection (outside the writer lock).
        monkeypatch.setattr(db, "_open_writer_conn", tracking_open)
        db._try_wal_checkpoint()
        execute_calls = [sql for conn in tracked for sql in conn.execute_calls]

        passive_calls = [c for c in execute_calls if "wal_checkpoint(PASSIVE)" in c]
        truncate_calls = [c for c in execute_calls if "wal_checkpoint(TRUNCATE)" in c]
        assert len(passive_calls) == 1, (
            f"Expected 1 PASSIVE checkpoint call, got {len(passive_calls)}"
        )
        assert len(truncate_calls) == 0, (
            "Periodic checkpoint should NOT use TRUNCATE"
        )

    def test_failed_checkpoint_never_raises_and_is_not_silent(self, db, caplog, monkeypatch):
        """The periodic checkpoint runs from the write path: a failing PASSIVE
        checkpoint must not raise into the caller, and must leave a WARNING."""
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(db, "_open_writer_conn", lambda: mock_conn)
        with caplog.at_level(logging.WARNING, logger="hermes_state"):
            db._try_wal_checkpoint()
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_writers_proceed_while_a_slow_checkpoint_runs(self, db, monkeypatch):
        """On macOS checkpoint_fullfsync makes a PASSIVE checkpoint wait for the whole device's
        dirty data. It must not hold the writer lock meanwhile: on 2026-10-11 20 turns and 41
        threads queued on SessionDB._lock behind one periodic checkpoint."""
        import threading

        entered, release = threading.Event(), threading.Event()

        class SlowCheckpoint(TrackingConnection):
            def execute(self, sql, *args, **kwargs):
                if "wal_checkpoint" in sql and threading.current_thread().name == "slow-checkpoint":
                    entered.set()
                    assert release.wait(10), "checkpoint never released"
                return super().execute(sql, *args, **kwargs)

        open_conn = db._open_writer_conn
        monkeypatch.setattr(db, "_open_writer_conn", lambda: SlowCheckpoint(open_conn()))
        monkeypatch.setattr(db, "_conn", SlowCheckpoint(db._conn))  # wherever it runs, it is slow
        checkpointer = threading.Thread(target=db._try_wal_checkpoint, name="slow-checkpoint")
        checkpointer.start()
        try:
            assert entered.wait(10), "checkpoint never started"
            writer = threading.Thread(target=db.set_meta, args=("probe", "1"))
            writer.start()
            writer.join(3)
            assert not writer.is_alive(), "a write waited on the periodic checkpoint"
        finally:
            release.set()
            checkpointer.join(10)
        assert db.get_meta("probe") == "1"




class TestCloseUsesPassive:
    """close() must use PASSIVE. Transient per-cron-run SessionDB connections
    close many times an hour; a TRUNCATE reset there races the live gateway
    writer on the large WAL DB and corrupts B-tree pages (#45383)."""

    def test_close_uses_passive_mode(self, db):
        """close() checkpoints PASSIVE, never TRUNCATE."""
        real_conn = db._conn
        execute_calls = []

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        db._conn = mock_conn

        db.close()

        truncate_calls = [c for c in execute_calls if "wal_checkpoint(TRUNCATE)" in c]
        passive_calls = [c for c in execute_calls if "wal_checkpoint(PASSIVE)" in c]
        assert len(truncate_calls) == 0, (
            "close() must NOT TRUNCATE (races the live gateway writer, #45383)"
        )
        assert len(passive_calls) == 1, (
            f"Expected 1 PASSIVE checkpoint at close, got {len(passive_calls)}"
        )

    def test_failed_checkpoint_at_close_still_releases_the_handle(self, db):
        """close() is best-effort about the checkpoint: a failing PASSIVE at
        close must neither raise nor leave the handle open."""
        real_conn = db._conn
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        db._conn = mock_conn
        try:
            db.close()
        finally:
            real_conn.close()

        assert db._conn is None
        mock_conn.close.assert_called()



class TestVacuumUsesPassive:
    """Manual vacuum paths must checkpoint PASSIVE, never TRUNCATE."""

    def test_vacuum_uses_passive_before_vacuum(self, db):
        """SessionDB.vacuum() checkpoints PASSIVE before VACUUM.

        A TRUNCATE checkpoint AFTER the VACUUM is expected: VACUUM rewrites
        every page through the WAL, so without it a 3 GB database leaves a
        3 GB state.db-wal behind and `sessions optimize` becomes a net disk
        LOSS. The #45383 tearing concern is about truncating while another
        writer is mid-transaction — the pre-VACUUM checkpoint stays PASSIVE
        for that reason; the post-VACUUM truncate runs under the same
        exclusive-lock window the VACUUM itself required.
        """
        real_conn = db._conn
        tracking_conn = TrackingConnection(real_conn)
        db._conn = tracking_conn

        db.vacuum()

        checkpoint_calls = [
            c for c in tracking_conn.execute_calls if "wal_checkpoint" in c.lower()
        ]
        truncate_calls = [c for c in checkpoint_calls if "TRUNCATE" in c]
        passive_calls = [c for c in checkpoint_calls if "PASSIVE" in c]
        vacuum_calls = [
            c for c in tracking_conn.execute_calls if c.strip().upper() == "VACUUM"
        ]
        assert passive_calls == ["PRAGMA wal_checkpoint(PASSIVE)"]
        assert vacuum_calls == ["VACUUM"]
        assert truncate_calls == ["PRAGMA wal_checkpoint(TRUNCATE)"]
        vacuum_index = tracking_conn.execute_calls.index(vacuum_calls[0])
        assert tracking_conn.execute_calls.index(passive_calls[0]) < vacuum_index
        # TRUNCATE must come only AFTER the VACUUM (never before it).
        assert tracking_conn.execute_calls.index(truncate_calls[0]) > vacuum_index

    def test_optimize_storage_uses_passive_after_vacuum(self, db):
        """optimize_fts_storage() checkpoints PASSIVE after its VACUUM."""
        real_conn = db._conn
        tracking_conn = TrackingConnection(real_conn)
        db._conn = tracking_conn

        result = db.optimize_fts_storage(vacuum=True)

        checkpoint_calls = [
            c for c in tracking_conn.execute_calls if "wal_checkpoint" in c.lower()
        ]
        truncate_calls = [c for c in checkpoint_calls if "TRUNCATE" in c]
        passive_calls = [c for c in checkpoint_calls if "PASSIVE" in c]
        vacuum_calls = [
            c for c in tracking_conn.execute_calls if c.strip().upper() == "VACUUM"
        ]
        assert result["ok"] is True
        assert result["vacuumed"] is True
        assert truncate_calls == []
        assert passive_calls == ["PRAGMA wal_checkpoint(PASSIVE)"]
        assert vacuum_calls == ["VACUUM"]
        assert tracking_conn.execute_calls.index(
            vacuum_calls[0]
        ) < tracking_conn.execute_calls.index(passive_calls[0])


class TestCheckpointFrequency:
    """Checkpoint triggers every N writes."""

    def test_checkpoint_triggers_at_interval(self, db):
        """_try_wal_checkpoint is called every _CHECKPOINT_EVERY_N_WRITES writes."""
        call_count = [0]
        original = db._try_wal_checkpoint

        def counting_checkpoint():
            call_count[0] += 1
            original()

        db._try_wal_checkpoint = counting_checkpoint

        # Write exactly _CHECKPOINT_EVERY_N_WRITES sessions to trigger one checkpoint
        n = db._CHECKPOINT_EVERY_N_WRITES
        import time as _time
        for i in range(n):
            db._execute_write(lambda conn, _i=i: conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                (f"sess_{_i}", "test", _time.time()),
            ))

        assert call_count[0] == 1, (
            f"Expected 1 checkpoint after {n} writes, got {call_count[0]}"
        )


class TestFtsMergeOffTheWriterLock:
    def test_writers_proceed_while_a_slow_fts_merge_runs(self, db, monkeypatch):
        """Each incremental FTS merge command commits on its own (synchronous=FULL). Under the
        writer lock a pass stalled every writer for seconds (2026-10-11 11:39: 19 turns and 48
        writers queued behind _merge_fts_incrementally)."""
        import threading

        entered, release = threading.Event(), threading.Event()

        class SlowMerge(TrackingConnection):
            def execute(self, sql, *args, **kwargs):
                if "'merge'" in sql and threading.current_thread().name == "slow-merge":
                    entered.set()
                    assert release.wait(10), "merge never released"
                return super().execute(sql, *args, **kwargs)

        open_conn = db._open_writer_conn
        monkeypatch.setattr(db, "_open_writer_conn", lambda: SlowMerge(open_conn()))
        monkeypatch.setattr(db, "_conn", SlowMerge(db._conn))  # wherever it runs, it is slow
        db.create_session(session_id="s1", source="cli")
        db.append_message(session_id="s1", role="user", content="merge me")
        merger = threading.Thread(target=db._try_incremental_merge_fts, name="slow-merge")
        merger.start()
        try:
            assert entered.wait(10), "merge never started"
            writer = threading.Thread(target=db.set_meta, args=("probe", "1"))
            writer.start()
            writer.join(3)
            assert not writer.is_alive(), "a write waited on the incremental FTS merge"
        finally:
            release.set()
            merger.join(10)
        assert db.get_meta("probe") == "1"
