"""Slow transcript warnings attribute phases without changing durable or caller row state."""

import os
import sqlite3
import time

import pytest

import hermes_state
from agent.message_metadata import CANONICAL_ROW, DB_ROW_SNAPSHOT
from hermes_state import SessionDB
from hermes_state_write_metrics import current_write_metrics


class _Clock:
    def __init__(self):
        self.wall = 0.0
        self.cpu = 0.0

    def monotonic(self):
        return self.wall

    def thread_time(self):
        return self.cpu

    def __getattr__(self, name):
        return getattr(time, name)


class _Connection:
    """Keep actual SQLite transactions, advancing only deterministic phase clocks."""

    def __init__(self, conn, clock, *, acquire=0.0, commit=0.0, rollback=0.0, rollback_fails=False):
        self.conn = conn
        self.clock = clock
        self.acquire = acquire
        self.commit_delay = commit
        self.rollback_delay = rollback
        self.rollback_fails = rollback_fails
        self.begins = self.commits = self.rollbacks = 0

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, *args):
        if sql == "BEGIN IMMEDIATE":
            self.begins += 1
            self.clock.wall += self.acquire
        return self.conn.execute(sql, *args)

    def commit(self):
        self.commits += 1
        self.clock.wall += self.commit_delay
        return self.conn.commit()

    def rollback(self):
        self.rollbacks += 1
        self.clock.wall += self.rollback_delay
        self.conn.rollback()
        if self.rollback_fails:
            raise RuntimeError("private rollback error")


@pytest.fixture
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    store.create_session("parent", "test")
    store.append_messages_batch("parent", [{"role": "user", "content": "original"}])
    try:
        yield store
    finally:
        store.close()


def _warnings(caplog):
    return [record.getMessage() for record in caplog.records
            if record.getMessage().startswith("state.db write transaction held the write lock")]


@pytest.mark.parametrize("operation", [
    "append_messages_batch", "replace_messages", "archive_and_compact", "publish_compression_child",
])
@pytest.mark.parametrize("acquire,commit", [(7.0, 4.0), (7.0, 0.0), (0.0, 0.0)])
def test_transcript_commit_attribution_and_quiet_fast_writes(db, monkeypatch, caplog, operation, acquire, commit):
    clock = _Clock()
    conn = _Connection(db._conn, clock, acquire=acquire, commit=commit)
    monkeypatch.setattr(db, "_conn", conn)
    monkeypatch.setattr(hermes_state, "time", clock)
    messages = [{"role": "user", "content": "private transcript payload"}]
    target = "child" if operation == "publish_compression_child" else "parent"
    with caplog.at_level("WARNING", logger="hermes_state"):
        if operation == "publish_compression_child":
            db.publish_compression_child(parent_session_id="parent", child_session_id=target,
                source="test", messages=messages, require_compression_lease=False)
        else:
            getattr(db, operation)(target, messages)
    assert conn.begins == conn.commits == 1
    assert conn.rollbacks == 0
    assert current_write_metrics.get() is None
    assert messages[0]["_row_id"] > 0 and DB_ROW_SNAPSHOT in messages[0]
    assert db.get_messages(target)[-1]["content"] == messages[0]["content"]
    warnings = _warnings(caplog)
    if not commit:
        assert warnings == []  # Acquisition delay alone must not be reported as lock hold.
        return
    assert len(warnings) == 1
    warning = warnings[0]
    assert operation in warning and "_attempt" not in warning
    for field in (f"pid={os.getpid()}", f"session='{target}'", "input_rows=1", "acquire_s=7.000",
                  "callback_s=0.000", "callback_cpu_s=0.000", "commit_s=4.000", "rollback_s=0.000",
                  "outcome=committed"):
        assert field in warning
    assert "for 4.0s:" in warning
    assert "commit=4.000s" in warning and "inserted=1" in warning and "fts=inline" in warning
    assert "private transcript payload" not in caplog.text
    assert "INSERT INTO" not in caplog.text


@pytest.mark.parametrize("outcome", ["failure", "retry", "rollback_failure"])
def test_transcript_callback_rollback_and_retry_keep_row_markers(db, monkeypatch, caplog, outcome):
    clock = _Clock()
    conn = _Connection(db._conn, clock, rollback=2.0, rollback_fails=outcome == "rollback_failure")
    monkeypatch.setattr(db, "_conn", conn)
    monkeypatch.setattr(hermes_state, "time", clock)
    monkeypatch.setattr(db, "_WRITE_RETRY_MIN_S", 0.001)
    monkeypatch.setattr(db, "_WRITE_RETRY_MAX_S", 0.001)
    insert = db._insert_message_rows
    attempts = []
    messages = [{"role": "user", "content": "private transcript payload", "timestamp": 123.0}]

    def insert_then_fail(*args, **kwargs):
        attempts.append(dict(messages[0]))
        result = insert(*args, **kwargs)
        clock.wall += 4.0
        clock.cpu += 1.0
        if len(attempts) == 1:
            if outcome == "retry":
                raise sqlite3.OperationalError("database is locked")
            raise ValueError("private callback error")
        return result

    monkeypatch.setattr(db, "_insert_message_rows", insert_then_fail)
    with caplog.at_level("WARNING", logger="hermes_state"):
        if outcome == "retry":
            assert db.append_messages_batch("parent", messages) == 1
        else:
            with pytest.raises(ValueError, match="private callback error"):
                db.append_messages_batch("parent", messages)
    warnings = _warnings(caplog)
    assert len(warnings) == (2 if outcome == "retry" else 1)
    for warning in warnings:
        assert "append_messages_batch" in warning and "_attempt" not in warning
        for field in ("session='parent'", "input_rows=1", "callback_s=4.000", "callback_cpu_s=1.000"):
            assert field in warning
    assert "rollback_s=2.000" in warnings[0]
    assert ("outcome=rollback_failed" if outcome == "rollback_failure" else "outcome=rolled_back") in warnings[0]
    assert conn.rollbacks == 1
    assert current_write_metrics.get() is None
    for warning in warnings:
        assert "callback_total=4.000s" in warning and "inserted=1" in warning
    assert "rollback=2.000s" in warnings[0]
    if outcome == "rollback_failure":
        assert "state.db rollback failed: error_type=RuntimeError" in caplog.text
    else:
        assert "state.db rollback failed:" not in caplog.text
    assert conn.begins == len(attempts) == (2 if outcome == "retry" else 1)
    if outcome == "retry":
        assert "outcome=committed" in warnings[-1] and "rollback_s=0.000" in warnings[-1]
        assert conn.commits == 1
        assert "_row_id" not in attempts[1] and DB_ROW_SNAPSHOT not in attempts[1]
        assert CANONICAL_ROW not in attempts[1] and attempts[1]["timestamp"] == 123.0
        assert messages[0]["_row_id"] > 0 and DB_ROW_SNAPSHOT in messages[0]
        assert [row["content"] for row in db.get_messages("parent")] == ["original", "private transcript payload"]
    else:
        assert conn.commits == 0
        assert "_row_id" not in messages[0] and DB_ROW_SNAPSHOT not in messages[0]
        assert CANONICAL_ROW not in messages[0] and messages[0]["timestamp"] == 123.0
        assert [row["content"] for row in db.get_messages("parent")] == ["original"]
    assert "private transcript payload" not in caplog.text
    assert "private callback error" not in caplog.text
    assert "private rollback error" not in caplog.text
    assert "INSERT INTO" not in caplog.text
