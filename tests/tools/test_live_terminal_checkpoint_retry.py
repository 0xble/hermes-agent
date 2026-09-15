"""Exact terminal checkpoints recover while their producing process remains alive."""
import queue
import sqlite3
import time

import pytest

from tools import async_delegation as delegation


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    delegation._completion_producer_owners.clear()
    delegation._completion_publications.clear()
    delegation._completion_retry_homes.clear()
    yield
    delegation._completion_producer_owners.clear()
    delegation._completion_publications.clear()
    delegation._completion_retry_homes.clear()


@pytest.mark.parametrize("status", ["completed", "budget_exhausted", "interrupted", "stalled"])
@pytest.mark.parametrize("failure", ["persist", "publish"])
def test_same_owner_retry_and_single_delivery_claim(monkeypatch, failure, status):
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
    delegation._push_completion_event(record, {"summary": "exact result"}, status)
    assert target.empty()
    monkeypatch.setattr(delegation, "_persist_completion", original)
    monkeypatch.setattr(target, "put", queue.Queue.put.__get__(target))
    assert delegation.retry_current_owner_terminal_checkpoints(target) == 1
    event = target.get_nowait()
    assert event["summary"] == "exact result"
    assert event["status"] == status
    with delegation._transaction() as conn:
        assert conn.execute("SELECT state FROM async_delegations WHERE delegation_id='retry-test'").fetchone()[0] == status
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


@pytest.mark.parametrize('dispatch_start,completion_start', [(None, 101), (101, None), (None, None)])
@pytest.mark.parametrize('persist_failure', [False, True])
def test_captured_producer_survives_missing_identity_lookup(monkeypatch, dispatch_start, completion_start, persist_failure):
    from pathlib import Path
    from gateway import status
    from tools.process_registry import process_registry
    assert Path(delegation.__file__).resolve().parents[1] == Path(__file__).resolve().parents[2]
    monkeypatch.setattr(status, 'get_process_start_time', lambda _: dispatch_start)
    record = {'delegation_id': 'identity-gap', 'session_key': 's', 'dispatched_at': time.time()}
    delegation._persist_dispatch(record)
    monkeypatch.setattr(status, 'get_process_start_time', lambda _: completion_start)
    target = queue.Queue()
    monkeypatch.setattr(process_registry, 'completion_queue', target)
    original = delegation._persist_completion
    if persist_failure:
        def unavailable(*args):
            raise sqlite3.OperationalError('locked')
        monkeypatch.setattr(delegation, '_persist_completion', unavailable)
    delegation._push_completion_event(record, {'summary': 'exact result'}, 'completed')
    monkeypatch.setattr(delegation, '_persist_completion', original)
    delegation.retry_current_owner_terminal_checkpoints(target)
    assert target.qsize() == 1
    assert target.get_nowait()['summary'] == 'exact result'
    assert not delegation._completion_producer_owners
    assert delegation.get_durable_delegation('identity-gap')['state'] == 'completed'
    assert not delegation._terminal_checkpoint_path('identity-gap').exists()
    assert delegation.retry_current_owner_terminal_checkpoints(target) == 0


@pytest.mark.parametrize('conflict', ['foreign_pid', 'known_start', 'stored_start', 'restart_null', 'foreign_profile'])
def test_captured_producer_cannot_override_foreign_identity(monkeypatch, tmp_path, conflict):
    from gateway import status
    from tools.process_registry import process_registry
    monkeypatch.setattr(status, 'get_process_start_time', lambda _: None if conflict in {'restart_null', 'foreign_profile'} else 101)
    record = {'delegation_id': 'identity-conflict', 'session_key': 's', 'dispatched_at': time.time()}
    original_db = delegation._db_path()
    delegation._persist_dispatch(record)
    if conflict == 'foreign_profile':
        monkeypatch.setattr(delegation, '_db_path', lambda: original_db)
        monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'other-profile'))
    elif conflict == 'restart_null':
        delegation._reset_for_tests()
        monkeypatch.setattr(status, 'get_process_start_time', lambda _: 101)
    elif conflict == 'known_start':
        monkeypatch.setattr(status, 'get_process_start_time', lambda _: 202)
    else:
        column = 'owner_pid' if conflict == 'foreign_pid' else 'owner_started_at'
        with delegation._transaction() as conn:
            conn.execute(f'UPDATE async_delegations SET {column}={column}+1')
    target = queue.Queue()
    monkeypatch.setattr(process_registry, 'completion_queue', target)
    delegation._push_completion_event(record, {'summary': 'must stay fenced'}, 'completed')
    delegation._completion_retry_homes.add(tmp_path.resolve())
    assert delegation.retry_current_owner_terminal_checkpoints(target) == 0
    assert target.empty()
    assert delegation.get_durable_delegation('identity-conflict')['state'] == 'running'
    assert delegation._terminal_checkpoint_path('identity-conflict').exists()
