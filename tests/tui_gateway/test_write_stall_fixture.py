"""The opt-in TUI fixture suppresses only native helper startup, not writes."""

import sqlite3
import threading

import hermes_state_write_stalls as stalls
from hermes_state import SessionDB


def test_without_sampler_preserves_state_writes(
    tmp_path, monkeypatch, without_write_stall_sampler,
):
    def unexpected_thread(*args, **kwargs):
        raise AssertionError("a gateway thread double must not launch the sampler")

    monkeypatch.setattr(threading, "Thread", unexpected_thread)
    watcher = stalls.WriteStallWatchdog()
    monkeypatch.setattr(stalls, "_watchdog", watcher)
    watcher.start()
    assert watcher._thread is None

    with SessionDB(db_path=tmp_path / "store.sqlite") as db:
        db._execute_write(lambda conn: conn.execute("CREATE TABLE proof (value TEXT)"))
        db._execute_write(lambda conn: conn.execute("INSERT INTO proof VALUES ('committed')"))
        with sqlite3.connect(db.db_path) as reader:
            assert reader.execute("SELECT value FROM proof").fetchall() == [("committed",)]
    assert not watcher._closed  # the fixture, not a fake DB, owns helper cleanup
