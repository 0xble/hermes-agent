"""Regression: the streamed-preview journal must close every SQLite connection.

Sibling of #69567 and tests/gateway/test_delivery_ledger_fd_leak.py. The journal used
``with conn:`` (commit, never close), so every stream edit leaked the state.db/-wal/-shm
file descriptors until GC; ``record_preview`` runs before and after every preview send
and edit, so a long-running gateway exhausts ``RLIMIT_NOFILE``.
"""

import gc
import sqlite3

import pytest

from gateway import delivery_ledger as dl
from gateway import stream_recovery as recovery
from tests.gateway.test_delivery_ledger_fd_leak import _TrackingConnection


@pytest.fixture
def tracked(monkeypatch, tmp_path):
    monkeypatch.setattr(dl, "_db_path", lambda: tmp_path / "state.db")
    opened, closed = [], []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(id(conn))
        return _TrackingConnection(conn, closed)

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    return opened, closed


def _exercise():
    recovery.record_preview(turn_id="t", session_key="k", session_id="s", content="a", message_id=None)
    recovery.record_preview(turn_id="t", session_key="k", session_id="s", content="ab", message_id="m1")
    assert recovery.load_preview("k", "s", "t") == {"content": "ab", "message_id": "m1"}
    recovery.discard_preview("t")
    assert recovery.load_preview("k", "s", "t") is None


@pytest.mark.parametrize("op", ["record_preview", "load_preview", "discard_preview"])
def test_each_journal_operation_closes_its_connection(tracked, op):
    opened, closed = tracked
    recovery.record_preview(turn_id="t", session_key="k", session_id="s", content="a", message_id="m1")
    del opened[:], closed[:]
    if op == "record_preview":
        recovery.record_preview(turn_id="t", session_key="k", session_id="s", content="ab", message_id="m1")
    elif op == "load_preview":
        recovery.load_preview("k", "s", "t")
    else:
        recovery.discard_preview("t")
    assert len(opened) == 1
    assert closed == opened


def test_journal_closes_on_error(tracked, monkeypatch):
    opened, closed = tracked
    with pytest.raises(sqlite3.Error):
        recovery._journal(lambda conn: conn.execute("SELECT * FROM no_such_journal"))
    assert opened and set(opened) == set(closed)


def test_many_journal_writes_hold_no_extra_fds(tracked):
    """The reviewer's shape: 50 writes with GC disabled must not accumulate connections."""
    opened, closed = tracked
    gc.disable()
    try:
        for i in range(50):
            recovery.record_preview(turn_id="t", session_key="k", session_id="s",
                                    content=f"edit {i}", message_id="m1")
        _exercise()
    finally:
        gc.enable()
    assert len(opened) == 55
    assert set(opened) == set(closed)
