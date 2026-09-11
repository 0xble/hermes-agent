"""Exact terminal checkpoints recover while their producing process remains alive."""
import queue
import sqlite3
import time

import pytest

from tools import async_delegation as delegation


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    delegation._completion_publications.clear()
    delegation._completion_retry_homes.clear()
    yield
    delegation._completion_publications.clear()
    delegation._completion_retry_homes.clear()


@pytest.mark.parametrize("failure", ["persist", "publish"])
def test_same_owner_retry_and_single_delivery_claim(monkeypatch, failure):
    from tools.process_registry import process_registry
    target = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", target)
    record = {"delegation_id": "retry-test", "session_key": "s", "goal": "g", "dispatched_at": time.time()}
    delegation._persist_dispatch(record)
    original = delegation._persist_completion
    if failure == "persist":
        monkeypatch.setattr(delegation, "_persist_completion", lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    else:
        monkeypatch.setattr(target, "put", lambda _: (_ for _ in ()).throw(RuntimeError("queue unavailable")))
    delegation._push_completion_event(record, {"summary": "exact result"}, "completed")
    assert target.empty()
    monkeypatch.setattr(delegation, "_persist_completion", original)
    monkeypatch.setattr(target, "put", queue.Queue.put.__get__(target))
    assert delegation.retry_current_owner_terminal_checkpoints(target) == 1
    event = target.get_nowait()
    assert event["summary"] == "exact result"
    assert delegation.retry_current_owner_terminal_checkpoints(target) == 0
    assert target.empty()
    assert delegation.claim_event_delivery(event, "first")
    assert delegation.claim_event_delivery(event, "duplicate") is None


def test_foreign_owner_checkpoint_is_not_imported(tmp_path):
    record = {"delegation_id": "foreign", "session_key": "s", "dispatched_at": time.time()}
    delegation._persist_dispatch(record)
    delegation._checkpoint_terminal_result({"delegation_id": "foreign", "status": "completed"}, {"summary": "result"})
    with delegation._transaction() as conn:
        conn.execute("UPDATE async_delegations SET owner_pid=owner_pid+1 WHERE delegation_id='foreign'")
    delegation._completion_retry_homes.add(tmp_path.resolve())
    target = queue.Queue()
    assert delegation.retry_current_owner_terminal_checkpoints(target) == 0
    assert target.empty()
    assert delegation._terminal_checkpoint_path("foreign").exists()
