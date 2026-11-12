"""The gateway event loop must not wait on locks that state.db writers hold.

Both waits below parked the live loop for seconds at a time (py-spy: 45% of loop time), which
delayed Telegram delivery and surfaced as spurious connect timeouts.
"""
from __future__ import annotations

import threading
import time

import hermes_loop_guard
from hermes_state import SessionDB
from tools import async_delegation


def _as_loop_thread(fn):
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    try:
        return fn()
    finally:
        hermes_loop_guard.set_loop_thread(None)


def test_token_flush_does_not_wait_for_a_busy_writer_on_the_loop_thread(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db._token_writer_busy = True  # a live writer mid-commit behind the state.db write lock
        started = time.monotonic()
        assert _as_loop_thread(lambda: db.flush_token_counts(timeout=2.0)) is False
        assert time.monotonic() - started < 0.5
    finally:
        db._token_writer_busy = False
        db.close()


def test_token_flush_still_waits_off_the_loop_thread(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db._token_writer_busy = True
        started = time.monotonic()
        assert db.flush_token_counts(timeout=0.3) is False
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
