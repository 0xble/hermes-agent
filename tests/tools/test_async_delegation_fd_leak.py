"""Regression: the async-delegation ledger must close every SQLite connection.

Sibling of the cron execution-ledger leak (#69567 / PR #69594). The durable
delegation ledger used ``with _connect() as conn:`` where the connection
context manager commits/rolls back but never closes, leaking the db/-wal/-shm
file descriptors on every dispatch, completion, and delivery-claim. These tests
fail if the deterministic ``close()`` is ever removed again.
"""

import queue
import sqlite3

import pytest

from tools import async_delegation as ad


class _TrackingConnection:
    """Close instrumentation mixed into the requested real connection factory.

    Keep sqlite_safe_read's tracked factory/close semantics intact; a delegating
    proxy is not itself a SQLite connection and cannot safely track its lifetime.
    """

    def close(self):
        self._closed_ids.append(id(self))
        super().close()


def _point_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    return ad


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

    monkeypatch.setattr(ad.sqlite3, "connect", tracking_connect)
    return opened, closed


def test_ledger_operations_close_every_connection(monkeypatch, tmp_path):
    """Public durable-ledger reads/writes must close every connection opened."""
    _point_ledger(monkeypatch, tmp_path)
    opened, closed = _track_connections(monkeypatch)

    ad.get_durable_delegation("nope")
    ad.recover_abandoned_delegations()
    ad.restore_undelivered_completions(queue.Queue())
    ad.claim_completion_delivery("nope", "claim-1")

    assert opened, "expected at least one connection to be opened"
    assert len(opened) == len(closed)
    assert set(opened) == set(closed)


class _FailingSchemaCursor:
    """Cursor proxy that fails the canonical DDL replay. ``apply_declared_schema``
    replays SCHEMA_SQL statement by statement through ``conn.cursor()`` (no
    ``executescript`` implicit COMMIT), so the injection sits on the cursor."""

    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def execute(self, sql, *args, **kwargs):
        if "CREATE TABLE" in sql:
            raise sqlite3.OperationalError("simulated schema init failure")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_schema_init_failure_still_closes_connection(monkeypatch, tmp_path):
    """A PRAGMA/DDL failure after connect() must still close the state.db connection.

    Only state.db opens are tracked: the column reconciler builds its reference
    shape from SCHEMA_SQL in a private ``:memory:`` database, which it closes itself
    and which holds no file descriptor."""
    _point_ledger(monkeypatch, tmp_path)
    opened, closed = [], []
    real_connect = sqlite3.connect

    class _FailingSchemaConnection(_TrackingConnection):
        def execute(self, sql, *args, **kwargs):
            if "CREATE TABLE" in sql:
                raise sqlite3.OperationalError("simulated schema init failure")
            return super().execute(sql, *args, **kwargs)

        def executescript(self, script):
            if "CREATE TABLE" in script:
                raise sqlite3.OperationalError("simulated schema init failure")
            return super().executescript(script)

        def cursor(self, *args, **kwargs):
            return _FailingSchemaCursor(super().cursor(*args, **kwargs))

    def tracking_connect(database, *args, **kwargs):
        if str(database) == ":memory:":
            return real_connect(database, *args, **kwargs)
        factory = kwargs.pop("factory", sqlite3.Connection)
        kwargs["factory"] = type("FailingObservedConnection", (_FailingSchemaConnection, factory), {})
        conn = real_connect(database, *args, **kwargs)
        conn._closed_ids = closed
        opened.append(id(conn))
        return conn

    monkeypatch.setattr(ad.sqlite3, "connect", tracking_connect)

    with pytest.raises(sqlite3.OperationalError, match="simulated schema init failure"):
        with ad._transaction():
            pass

    assert len(opened) == 1
    assert closed == opened


def test_post_open_hardening_failure_closes_connection(monkeypatch, tmp_path):
    """The second ``_secure_state_db_files`` pass runs after the connection is open;
    when it raises (ELOOP on a swapped-in sidecar link, PermissionError), the
    connection must be closed, not leaked with its db/-wal/-shm descriptors."""
    import errno

    import hermes_state

    _point_ledger(monkeypatch, tmp_path)
    opened, closed = [], []
    real_connect = sqlite3.connect

    def tracking_connect(database, *args, **kwargs):
        if str(database) == ":memory:":  # the reconciler's private reference shape
            return real_connect(database, *args, **kwargs)
        # Track through the requested factory, as _track_connections does: a delegating proxy is
        # not a SQLite connection.
        factory = kwargs.pop("factory", sqlite3.Connection)
        kwargs["factory"] = type("ObservedConnection", (_TrackingConnection, factory), {})
        conn = real_connect(database, *args, **kwargs)
        conn._closed_ids = closed
        opened.append(id(conn))
        return conn

    monkeypatch.setattr(ad.sqlite3, "connect", tracking_connect)
    real_secure = hermes_state._secure_state_db_files

    def secure_then_fail_after_open(path, *, create_main=False):
        real_secure(path, create_main=create_main)
        if not create_main:
            raise OSError(errno.ELOOP, "simulated swapped-in sidecar link", str(path))

    monkeypatch.setattr(hermes_state, "_secure_state_db_files", secure_then_fail_after_open)

    with pytest.raises(OSError, match="simulated swapped-in sidecar link"):
        ad.open_state_db(tmp_path / "state.db", "state.db (test)")

    assert len(opened) == 1
    assert closed == opened
