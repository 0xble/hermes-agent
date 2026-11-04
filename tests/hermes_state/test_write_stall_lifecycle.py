"""Diagnostic teardown must never prevent a state transaction from committing."""

import contextvars
import sqlite3
import threading

import pytest

import hermes_state_write_stalls as stalls
from hermes_state import SessionDB


@pytest.fixture
def watched_db(tmp_path, monkeypatch):
    # Exercise real slot publication without starting a sampler for these short writes.
    monkeypatch.setattr(stalls, "_watchdog", None)
    monkeypatch.setattr(stalls.WriteStallWatchdog, "start", lambda self: None)
    with SessionDB(db_path=tmp_path / "store.sqlite") as db:
        db._execute_write(lambda conn: conn.execute("CREATE TABLE proof (value TEXT)"))
        watcher = stalls._get_watchdog()
        try:
            yield db, watcher
        finally:
            watcher.close()
            if stalls._watchdog is not None:
                stalls._watchdog.close()


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("failure", ["close", "reserve", "startup"])
def test_write_after_watchdog_close_commits(watched_db, monkeypatch, failure):
    db, watcher = watched_db

    def unavailable(*args):
        raise OSError("diagnostic unavailable")

    if failure == "close":
        watcher.close()
    elif failure == "reserve":
        monkeypatch.setattr(watcher, "reserve", unavailable)
    else:
        monkeypatch.setattr(stalls, "_get_watchdog", unavailable)

    db._execute_write(lambda conn: conn.execute("INSERT INTO proof VALUES ('after close')"))

    with sqlite3.connect(db.db_path) as reader:
        assert reader.execute("SELECT value FROM proof").fetchall() == [("after close",)]
    if failure == "close":
        assert stalls._watchdog is not watcher


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("failure", ["close", "phase", "end", "flush", "finish"])
def test_watchdog_close_during_write_does_not_roll_back(watched_db, monkeypatch, failure):
    db, watcher = watched_db

    def unavailable(*args):
        raise ValueError("diagnostic unavailable")

    def write(conn):
        conn.execute("INSERT INTO proof VALUES ('during close')")
        if failure == "close":
            watcher.close()
        else:
            monkeypatch.setattr(stalls.current_observation.get(), failure, unavailable)
        return "written"

    assert db._execute_write(write) == "written"
    with sqlite3.connect(db.db_path) as reader:
        assert reader.execute("SELECT value FROM proof").fetchall() == [("during close",)]


def test_observation_outside_write_is_noop(tmp_path):
    def outside_write():
        observation = stalls.current_observation.get()
        observation.phase("callback")
        observation.end()
        with stalls.write_attempt():
            pass
        assert not (tmp_path / "logs" / "write-stalls").exists()

    contextvars.Context().run(outside_write)


@pytest.mark.platforms("macos")
def test_close_does_not_wait_for_in_progress_sample(monkeypatch):
    watcher = stalls.WriteStallWatchdog()
    entered, release = threading.Event(), threading.Event()

    def sample():
        entered.set()
        release.wait(timeout=10)

    # Model the serving thread's child.wait while the helper is sampling.
    watcher._thread = threading.Thread(target=sample, daemon=True)
    watcher._thread.start()
    assert entered.wait(timeout=5)
    closed = threading.Event()
    closer = threading.Thread(target=lambda: (watcher.close(), closed.set()), daemon=True)
    closer.start()
    try:
        assert closed.wait(timeout=2), "watchdog close waited for the helper's sample"
        assert watcher._thread.is_alive()
    finally:
        release.set()
        closer.join(timeout=10)
        watcher._thread.join(timeout=10)
        watcher.close()


@pytest.mark.platforms("macos")
def test_dead_helper_can_restart_after_cooldown(tmp_path, monkeypatch):
    from tests.hermes_state.test_transcript_write_diagnostics import _Clock
    from tests.waiting import wait_until

    clock = _Clock()
    monkeypatch.setattr(stalls, "time", clock)
    monkeypatch.setattr(stalls, "_watchdog", None)
    monkeypatch.setattr(stalls, "_start_failure_at", float("-inf"))
    children = []

    class Helper:
        def __init__(self, *args, **kwargs):
            self.exit = threading.Event()
            children.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def wait(self):
            self.exit.wait(timeout=10)
            return 1

    monkeypatch.setattr(stalls.subprocess, "Popen", Helper)
    watcher = stalls._get_watchdog()
    try:
        assert watcher is not None
        children[0].exit.set()
        assert wait_until(lambda: not watcher._thread.is_alive(), timeout=5)
        assert stalls._get_watchdog() is None
        assert len(children) == 1
        clock.wall += stalls._COOLDOWN_S + 1
        replacement = stalls._get_watchdog()
        assert replacement is not None and replacement is not watcher
        assert len(children) == 2
    finally:
        for child in children:
            child.exit.set()
        watcher.close()
        if stalls._watchdog is not None:
            stalls._watchdog.close()
