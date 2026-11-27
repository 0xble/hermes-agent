"""Regression: the gateway delivery ledger must close every SQLite connection.

Sibling of the cron execution-ledger leak (#69567 / PR #69594). The ledger used
``with _connect() as conn:`` where ``sqlite3.Connection.__exit__`` commits or
rolls back but never closes, leaking the db/-wal/-shm file descriptors on every
call until a long-running gateway exhausts ``RLIMIT_NOFILE``. ``record_obligation``
runs on every outbound final response, so this is the highest-frequency leaker of
the set. These tests fail if the deterministic ``close()`` is ever removed again.
"""

import sqlite3

import pytest

from gateway import delivery_ledger as dl


class _TrackingConnection:
    """Close instrumentation mixed into the requested real connection factory.

    Keep sqlite_safe_read's tracked factory/close semantics intact; a delegating
    proxy is not itself a SQLite connection and cannot safely track its lifetime.
    """

    def close(self):
        self._closed_ids.append(id(self))
        super().close()


def _point_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(dl, "_db_path", lambda: tmp_path / "state.db")
    return dl


def _track_connections(monkeypatch):
    opened, closed = [], []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        factory = kwargs.pop("factory", sqlite3.Connection)
        kwargs["factory"] = type("ObservedConnection", (_TrackingConnection, factory), {})
        conn = real_connect(*args, **kwargs)
        conn._closed_ids = closed
        opened.append(id(conn))
        return conn

    monkeypatch.setattr(dl.sqlite3, "connect", tracking_connect)
    return opened, closed


def test_ledger_operations_close_every_connection(monkeypatch, tmp_path):
    """Every public ledger operation must close the connection it opened."""
    _point_ledger(monkeypatch, tmp_path)
    opened, closed = _track_connections(monkeypatch)

    oid = dl.compute_obligation_id("sess", "msg", "content")
    dl.record_obligation(
        obligation_id=oid, session_key="sess", platform="telegram",
        chat_id="123", thread_id=None, content="hello",
    )
    dl.mark_attempting(oid)
    dl.mark_delivered(oid)
    dl.sweep_recoverable()

    assert opened, "expected at least one connection to be opened"
    assert len(opened) == len(closed)
    assert set(opened) == set(closed)


