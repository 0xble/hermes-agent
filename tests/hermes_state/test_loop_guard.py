"""The gateway loop thread must never wait on state.db: guarded locks report it.

Under pytest a guarded acquisition (or a SessionDB open) on the registered loop thread raises, so
a new loop-thread call site fails its own test. In production it is logged once per call site and
the lock is still taken: the report must never become the crash it describes.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

import hermes_loop_guard
from hermes_loop_guard import LoopGuardedLock, LoopThreadBlockingError


@pytest.fixture(autouse=True)
def _disarmed():
    hermes_loop_guard._reset_for_tests()
    yield
    hermes_loop_guard._reset_for_tests()


def test_registered_loop_thread_acquisition_raises_under_pytest():
    lock = LoopGuardedLock("test lock")
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    with pytest.raises(LoopThreadBlockingError, match="test lock on the gateway event-loop thread"):
        with lock:
            pass
    assert not lock.locked()
    assert lock.acquire(blocking=False)  # a non-blocking try never waits, so it is allowed
    lock.release()


def test_other_threads_and_disarmed_guard_take_the_lock():
    lock = LoopGuardedLock("test lock")
    with lock:  # disarmed
        pass
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    taken = []

    def worker():
        with lock:
            taken.append(True)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(5)
    assert taken == [True]


def test_production_logs_once_per_call_site_and_still_locks(monkeypatch, caplog):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    lock = LoopGuardedLock("test lock")
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    with caplog.at_level(logging.WARNING, logger="hermes_loop_guard"):
        for _ in range(3):
            with lock:
                assert lock.locked()
    warnings = [r for r in caplog.records if "test lock on the gateway event-loop thread" in r.getMessage()]
    assert len(warnings) == 1
    assert __file__ in warnings[0].getMessage()


def test_session_db_open_and_write_on_the_loop_thread_raise(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        async def on_loop():
            hermes_loop_guard.set_loop_thread(threading.get_ident())
            try:
                with pytest.raises(LoopThreadBlockingError, match="SessionDB open"):
                    SessionDB(db_path=tmp_path / "state.db")
                with pytest.raises(LoopThreadBlockingError, match="SessionDB writer lock"):
                    db.set_meta("probe", "1")
                await asyncio.to_thread(db.set_meta, "probe", "2")  # the supported route
            finally:
                hermes_loop_guard.set_loop_thread(None)

        asyncio.run(on_loop())
        assert db.get_meta("probe") == "2"
    finally:
        db.close()


def test_session_store_lock_is_guarded(tmp_path):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    with pytest.raises(LoopThreadBlockingError, match="session-store lock"):
        store._ensure_loaded()
