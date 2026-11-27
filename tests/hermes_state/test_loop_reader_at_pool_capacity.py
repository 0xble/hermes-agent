"""At read-pool capacity the gateway event-loop thread reads on its own connection, never the writer's.

``_read_ctx`` degrades to the locked writer connection when every pooled permit is out. On the gateway
loop that wait froze every adapter behind in-flight writes: on 2026-11-27 the pool hit capacity dozens
of times an hour, and the loop guard named ``logical_owner``, ``physical_target``, ``control_snapshot``
and the viewer automation reads, all plain reads on the loop.
"""
import threading
import time

import pytest

from hermes_loop_guard import set_loop_thread
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    handle = SessionDB(tmp_path / "state.db")
    handle.create_session("s", source="test")
    handle.set_meta("goal:s", "{}")
    yield handle
    handle.close()


@pytest.fixture()
def on_loop():
    set_loop_thread(threading.get_ident())
    yield
    set_loop_thread(None)


def _exhaust(monkeypatch, db):
    assert db._wal_active, "the fixture database runs in WAL mode"
    while db._evict_one_idle_read_conn():
        pass
    monkeypatch.setattr(db._read_budget, "acquire", lambda requester: False)


def _hold_writer(db):
    held, release = threading.Event(), threading.Event()

    def hold():
        with db._lock:
            held.set()
            release.wait(timeout=30)

    worker = threading.Thread(target=hold, daemon=True)
    worker.start()
    assert held.wait(timeout=10)
    return release, worker


def test_loop_read_at_capacity_does_not_wait_on_a_held_writer(db, monkeypatch, on_loop):
    _exhaust(monkeypatch, db)
    release, worker = _hold_writer(db)
    try:
        started = time.monotonic()
        assert db.get_session("s") is not None
        assert db.get_meta("goal:s") == "{}"
        assert time.monotonic() - started < 2.0
    finally:
        release.set()
        worker.join(timeout=10)


def test_loop_meta_read_does_not_wait_on_a_held_writer(db, on_loop):
    """Goal, loop and heartbeat snapshots read meta on the loop, pool free or not."""
    release, worker = _hold_writer(db)
    try:
        started = time.monotonic()
        assert db.get_meta("goal:s") == "{}"
        assert time.monotonic() - started < 2.0
    finally:
        release.set()
        worker.join(timeout=10)


def test_admission_read_at_capacity_on_the_loop_does_not_wait_on_a_held_writer(db, monkeypatch, on_loop):
    """The typed-message admission re-read stays synchronous (atomic with its waiter) and never waits."""
    from hermes_state_runtime import get_session_admission
    _exhaust(monkeypatch, db)
    release, worker = _hold_writer(db)
    try:
        started = time.monotonic()
        assert get_session_admission(db, admission_id="missing") is None
        assert time.monotonic() - started < 2.0
    finally:
        release.set()
        worker.join(timeout=10)
