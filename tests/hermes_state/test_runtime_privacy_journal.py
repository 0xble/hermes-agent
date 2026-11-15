"""Producer provenance guards journals, while authoritative refusal is never an ACK."""

from contextvars import copy_context
import json
from pathlib import Path

import pytest

from agent.incognito import incognito_scope, register_session
from agent.runtime_session_store import WorkerPersistenceError
from hermes_state_runtime import RuntimeStoreError


def _capture_saves(store, monkeypatch):
    saved, save = [], store._save

    def traced_save(journal):
        encoded = json.dumps(journal).encode()
        result = save(journal)
        # Keep every completed save in RAM, including files later overwritten/deleted.
        saved.append(encoded)
        return result

    monkeypatch.setattr(store, '_save', traced_save)
    return saved


def _disk_contains(root, content):
    return any(content.encode() in path.read_bytes() for path in root.rglob('*') if path.is_file())


@pytest.mark.parametrize('origin', ['scope', 'copied_private', 'revoked_public'])
def test_private_producer_never_journals_even_after_boundary_exits(worker, monkeypatch, origin):
    db, store = worker
    public = 'PUBLIC_journal_scanner_positive_control'
    private = 'PRIVATE_producer_journal_canary'
    saved = _capture_saves(store, monkeypatch)
    assert store.append_messages_batch('owned', [{'role': 'user', 'content': public}]) == 1
    root = Path(db.db_path).parent
    assert any(public.encode() in value for value in saved)
    assert _disk_contains(root, public)
    before = tuple(db._conn.iterdump())
    journal_before = json.loads(json.dumps(store.journal))
    saves_before = list(saved)
    calls, sql = [], []
    rpc = store.rpc

    def traced_rpc(method, **params):
        calls.append(method)
        return rpc(method, **params)

    monkeypatch.setattr(store, 'rpc', traced_rpc)

    def private_callback():
        with pytest.raises((WorkerPersistenceError, RuntimeStoreError), match='^privacy_denied$'):
            store._apply('transcript.append', {
                'messages': [{'role': 'user', 'content': private}], 'turn_lease_holder': None,
            })

    try:
        if origin == 'scope':
            context = None
        elif origin == 'copied_private':
            register_session('owned')
            with incognito_scope(True, 'owned'):
                context = copy_context()
            register_session('owned', False)
        else:
            with incognito_scope(False, 'owned'):
                context = copy_context()
            register_session('owned')
            register_session('owned', False)
        db._conn.set_trace_callback(sql.append)
        try:
            if context is None:
                with incognito_scope(True, 'owned'):
                    private_callback()
            else:
                context.run(private_callback)
        finally:
            db._conn.set_trace_callback(None)
        assert sql == []
        assert calls == []
        assert tuple(db._conn.iterdump()) == before
        assert store.journal == journal_before
        assert saved == saves_before
        assert not any(private.encode() in value for value in saved)
        assert not _disk_contains(root, private)
        assert store.retry_pending() == []
        assert not calls
        followup = [{'role': 'assistant', 'content': 'PUBLIC_after_private_callback'}]
        assert store.append_messages_batch('owned', followup) == 1
        assert followup[0]['_row_id'] == db.get_messages('owned')[-1]['id']
        assert store.journal['next_sequence'] == 3
        assert store.journal['pending'] == []
        assert db._read_one('SELECT last_sequence FROM worker_executions')[0] == 2
    finally:
        register_session('owned', False)


def test_owner_refusal_of_public_producer_is_non_ack_and_next_public_replay_works(worker, monkeypatch):
    db, store = worker
    saved = _capture_saves(store, monkeypatch)
    prefix = 'PUBLIC_before_owner_refusal'
    rejected = 'PUBLIC_producer_bytes_refused_by_owner'
    assert store.append_messages_batch('owned', [{'role': 'user', 'content': prefix}]) == 1
    root = Path(db.db_path).parent
    assert any(prefix.encode() in value for value in saved)
    assert _disk_contains(root, prefix)
    before = tuple(db._conn.iterdump())
    rpc, calls, sql = store.rpc, [], []

    def refusing_owner(method, **params):
        calls.append(params)
        # These bytes originated publicly. Exact public journal-before-send is required.
        assert any(rejected.encode() in value for value in saved)
        assert _disk_contains(store.path.parent, rejected)
        register_session('owned')
        db._conn.set_trace_callback(sql.append)
        try:
            return rpc(method, **params)
        finally:
            db._conn.set_trace_callback(None)
            register_session('owned', False)

    monkeypatch.setattr(store, 'rpc', refusing_owner)
    with pytest.raises((WorkerPersistenceError, RuntimeStoreError), match='^privacy_denied$'):
        store._apply('transcript.append', {
            'messages': [{'role': 'user', 'content': rejected}], 'turn_lease_holder': None,
        })
    assert len(calls) == 1
    assert sql == []
    assert tuple(db._conn.iterdump()) == before
    assert store.journal['pending'] == []
    assert store.journal['next_sequence'] == 2
    assert store.retry_pending() == []
    assert len(calls) == 1

    monkeypatch.setattr(store, 'rpc', rpc)
    public = [{'role': 'assistant', 'content': 'PUBLIC_after_owner_refusal'}]
    assert store.append_messages_batch('owned', public) == 1
    assert public[0]['_row_id'] == db.get_messages('owned')[-1]['id']
    # Replay exactly the committed public payload, without the caller's row annotations.
    receipt = calls[0] | {'payload': {
        'messages': [{'role': 'assistant', 'content': public[0]['content']}], 'turn_lease_holder': None,
    }}
    result = rpc('worker.persist', **receipt)
    assert result['count'] == 1
    assert len(result['annotations']) == 1
    assert result['annotations'][0]['_row_id'] == public[0]['_row_id']
    assert [message['content'] for message in db.get_messages('owned')] == [prefix, public[0]['content']]
    assert [row[0] for row in db._read_all('SELECT sequence FROM worker_receipts ORDER BY sequence')] == [1, 2]
    assert db._read_one('SELECT last_sequence FROM worker_executions')[0] == 2
    assert store.journal['next_sequence'] == 3
    assert store.journal['pending'] == []
