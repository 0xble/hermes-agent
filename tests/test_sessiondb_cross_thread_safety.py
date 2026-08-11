"""Cross-thread safety of SessionDB's public read paths.

Regression coverage for the incident where a long-running gateway logged
``Session DB append_message failed`` with a ``TrackedConnection`` that
"returned NULL without setting an exception": several public read methods
executed directly against ``self._conn`` — the *shared writer* — while a
worker thread was mid-write on the same connection. sqlite3 objects are only
serialized when the caller serializes them; interleaving a read into another
thread's write scrambles the connection's error state and can poison it for
the rest of the process.

These tests use real temporary SQLite databases (not mocks) because the
contract under test is a property of actual connection objects, and they use
barriers/events rather than sleeps so the overlap is deterministic.
"""

import threading

import pytest

from hermes_state import SessionDB


# The four public reads that historically ran unlocked against the writer.
# Each entry is (name, callable taking a SessionDB) and must be safe to call
# concurrently with append_message().
READ_PROBES = [
    ("get_compression_lock_holder", lambda db: db.get_compression_lock_holder("s1")),
    ("clear_session_activity_labels", lambda db: db.clear_session_activity_labels("s1")),
    ("get_handoff_state", lambda db: db.get_handoff_state("s1")),
    ("list_pending_handoffs", lambda db: db.list_pending_handoffs()),
]


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    database.create_session("s1", source="test")
    try:
        yield database
    finally:
        database.close()


def _append(database, session_id, n):
    database.append_message(session_id, "user", f"probe message {n}")


# ── The race itself ──


@pytest.mark.parametrize("name,probe", READ_PROBES, ids=[p[0] for p in READ_PROBES])
def test_public_read_does_not_race_writer(db, name, probe):
    """Concurrent append_message() + public read raise nothing and stay sane."""
    errors = []
    start = threading.Barrier(2)

    def writer():
        start.wait(timeout=10)
        try:
            for i in range(200):
                _append(db, "s1", i)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(("writer", exc))

    def reader():
        start.wait(timeout=10)
        try:
            for _ in range(200):
                probe(db)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(("reader", exc))

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), f"{name}: thread did not finish"

    assert errors == [], f"{name}: escaped exceptions {errors}"
    # The writer's work must actually be durable, not merely exception-free.
    assert db.message_count("s1") == 200


@pytest.mark.parametrize("name,probe", READ_PROBES, ids=[p[0] for p in READ_PROBES])
def test_public_read_never_touches_writer_connection_unlocked(db, name, probe):
    """The audited reads must not execute on self._conn without _lock.

    Asserted structurally rather than by timing: we wrap the writer connection
    so any execute() arriving while _lock is *not* held by the calling thread
    is recorded as a violation.
    """
    violations = []
    real_execute = db._conn.execute
    lock = db._lock

    def guarded_execute(*args, **kwargs):
        # _lock is a plain Lock, so "is it held" is the best available signal;
        # combined with the single-threaded call below, an unlocked execute
        # here can only have come from the method under test.
        if lock.acquire(blocking=False):
            lock.release()
            violations.append(args[0] if args else "<no sql>")
        return real_execute(*args, **kwargs)

    db._conn.execute = guarded_execute
    try:
        probe(db)
    finally:
        db._conn.execute = real_execute

    assert violations == [], f"{name} executed on the writer without _lock: {violations}"


def test_non_wal_read_waits_for_writer_lock(tmp_path):
    """Without WAL the reads fall back to the writer *under* _lock, not beside it."""
    database = SessionDB(tmp_path / "state.db")
    database.create_session("s1", source="test")
    # Force the legacy locked path regardless of the host's journal mode.
    database._wal_active = False
    try:
        holding = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def hold_lock():
            with database._lock:
                holding.set()
                release.wait(timeout=10)

        def read():
            holding.wait(timeout=10)
            database.get_handoff_state("s1")
            finished.set()

        holder = threading.Thread(target=hold_lock)
        reader = threading.Thread(target=read)
        holder.start()
        reader.start()

        assert holding.wait(timeout=10)
        # The reader must block while the lock is held rather than reaching
        # into the writer connection concurrently.
        assert not finished.wait(timeout=0.5), "read proceeded while _lock was held"
        release.set()
        assert finished.wait(timeout=10), "read never completed after lock release"

        holder.join(timeout=10)
        reader.join(timeout=10)
    finally:
        database.close()


def test_wal_reads_do_not_wait_on_writer_lock(db):
    """Under WAL the same reads complete on their own connection while _lock is held."""
    if not db._wal_active:
        pytest.skip("host SQLite did not enable WAL")

    done = threading.Event()
    result = {}

    def read():
        result["value"] = db.get_handoff_state("s1")
        done.set()

    with db._lock:
        reader = threading.Thread(target=read)
        reader.start()
        assert done.wait(timeout=10), "WAL read convoyed behind the writer lock"

    reader.join(timeout=10)
    assert result["value"] is None or isinstance(result["value"], dict)
