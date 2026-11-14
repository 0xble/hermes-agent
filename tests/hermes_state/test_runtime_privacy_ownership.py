"""Ownership fences precede worker journals and native receipt mutations."""
import json
from pathlib import Path

import pytest

from agent.incognito import incognito_scope, register_session
from agent.runtime_session_store import WorkerPersistenceError
import hermes_state_runtime as rt


PRIVATE = 'synthetic-private-owner'
CANARY = 'PRIVATE_BYTES_ownership_canary'
PUBLIC = 'PUBLIC_BYTES_positive_control'


def snapshot(db):
    return tuple(db._conn.iterdump())


def disk(root):
    return b''.join(p.read_bytes() for p in Path(root).rglob('*') if p.is_file())


@pytest.mark.parametrize('boundary', ['scope', 'registered', 'unavailable', 'parent', 'child', 'target'])
def test_worker_private_work_never_advances_or_replays_and_public_continues(worker, monkeypatch, boundary):
    db, store = worker
    if boundary in {'parent', 'target'}:
        # A real reserved parent/ancestor, rather than an unrelated identity that
        # the worker handler would independently reject as permission_denied.
        db.create_session(PRIVATE, 'cli')
        db._execute_write(lambda conn: conn.execute(
            'UPDATE sessions SET parent_session_id=? WHERE id=?', (PRIVATE, 'owned')))
        if boundary == 'target':
            db.end_session(PRIVATE, 'compression')
    elif boundary == 'child':
        assert db.try_acquire_compression_lock('owned', 'holder')
    before = snapshot(db)
    writes, calls, sql = [], [], []
    save, rpc = store._save, store.rpc

    def traced_save(journal):
        encoded = json.dumps(journal)
        result = save(journal)
        writes.append(encoded)
        return result

    def traced_rpc(method, **params):
        calls.append(params)
        return rpc(method, **params)

    monkeypatch.setattr(store, '_save', traced_save)
    monkeypatch.setattr(store, 'rpc', traced_rpc)
    rows = [{'role': 'user', 'content': CANARY}, {'role': 'assistant', 'content': CANARY}]
    try:
        db._conn.set_trace_callback(sql.append)
        with monkeypatch.context() as patch:
            if boundary == 'registered':
                register_session('owned')
            elif boundary in {'parent', 'child', 'target'}:
                register_session(PRIVATE)
            elif boundary == 'unavailable':
                def unavailable(*_args):
                    raise RuntimeError(CANARY)
                patch.setattr('agent.incognito.is_incognito', unavailable)
            with incognito_scope(boundary == 'scope') if boundary == 'scope' else _neutral():
                if boundary == 'parent':
                    operation, payload = 'session.create', {'source': 'cli', 'parent_session_id': PRIVATE, 'system_prompt': CANARY}
                elif boundary == 'child':
                    operation, payload = 'compression.publish', {
                        'child_session_id': PRIVATE, 'source': 'cli', 'messages': rows,
                        'model': None, 'model_config': None, 'system_prompt': CANARY,
                        'cwd': None, 'profile_name': None, 'compression_lock_holder': 'holder',
                        'require_compression_lease': True, 'require_lease_refresh': False,
                        'lease_ttl_seconds': 300.0, 'watermark': None, 'watermark_ceiling': None,
                    }
                elif boundary == 'target':
                    operation, payload = 'compression.cleanup', {'target': PRIVATE, 'holder': CANARY}
                else:
                    operation, payload = 'transcript.append', {'messages': rows, 'turn_lease_holder': None}
                if boundary in {'scope', 'registered'}:
                    assert store.append_messages_batch('owned', rows) == 0
                    assert store.append_message('owned', 'user', CANARY) == 0
                    store.queue_token_counts('owned', input_tokens=4, model=CANARY)
                else:
                    with pytest.raises((WorkerPersistenceError, rt.RuntimeStoreError), match='privacy_denied'):
                        store._apply(operation, payload)
        db._conn.set_trace_callback(None)
        assert sql == []
        assert snapshot(db) == before
        assert store.journal['pending'] == []
        assert store.journal['next_sequence'] == 1
        assert not any('_row_id' in row or '_db_persisted' in row for row in rows)
        assert not calls
        assert not writes
        assert CANARY.encode() not in disk(Path(db.db_path).parent)
        assert store.retry_pending() == []
        assert not calls
        register_session('owned', False)
        register_session(PRIVATE, False)
        monkeypatch.setattr(store, 'rpc', rpc)
        public = [{'role': 'user', 'content': PUBLIC}]
        assert store.append_messages_batch('owned', public) == 1
        assert db._read_one('SELECT last_sequence FROM worker_executions')[0] == 1
        assert store.journal['next_sequence'] == 2
        assert PUBLIC.encode() in disk(Path(db.db_path).parent)
        assert any(PUBLIC in value for value in writes)
    finally:
        db._conn.set_trace_callback(None)
        register_session('owned', False)
        register_session(PRIVATE, False)


def _neutral():
    from contextlib import nullcontext
    return nullcontext()


@pytest.mark.parametrize('private_owner', [False, True])
@pytest.mark.parametrize('operation', ['admit', 'settle', 'rename', 'import', 'lineage'])
def test_native_content_is_public_but_secondary_owners_refuse_before_writes(worker, operation, private_owner):
    db, store = worker
    epoch = store.scope['epoch']
    try:
        db.create_session('native', 'cli')
        if operation in {'settle', 'lineage'}:
            row = rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='native',
                                         request_id='input', payload={'text': 'public input'})
            row = rt.claim_session_input(db, epoch=epoch, session_id='native')
            if operation == 'lineage':
                child = PRIVATE if private_owner else 'public-continuation'
                scope = dict(epoch=epoch, execution_id='native-worker', session_id='native', generation=row['generation'])
                rt.register_worker_execution(db, **scope, kind='compute', adoption_secret='fixture-secret')
                assert db.try_acquire_compression_lock('native', 'holder')
                rt.mutate_worker_execution(db, **scope, sequence=1, operation='compression.publish', payload={
                    'child_session_id': child, 'source': 'cli',
                    'messages': [{'role': 'user', 'content': 'PUBLIC_prefix_handoff'}], 'model': None,
                    'model_config': None, 'system_prompt': 'PUBLIC_prefix', 'cwd': None, 'profile_name': None,
                    'compression_lock_holder': 'holder', 'require_compression_lease': True,
                    'require_lease_refresh': False, 'lease_ttl_seconds': 300.0,
                    'watermark': None, 'watermark_ceiling': None,
                })
                assert json.loads(db._read_one('SELECT lineage_json FROM session_admissions WHERE admission_id=?',
                                              (row['admission_id'],))[0]) == ['native', child]
        register_session(PRIVATE)
        if private_owner and operation in {'admit', 'settle', 'rename'}:
            register_session('native')
        before = snapshot(db)
        sql = []
        db._conn.set_trace_callback(sql.append)
        if operation == 'admit':
            call = lambda: rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='native',
                request_id='mention', payload={'text': PRIVATE, 'metadata': {'session_id': PRIVATE}})
        elif operation in {'settle', 'lineage'}:
            call = lambda: rt.settle_session_input(db, epoch=epoch, admission_id=row['admission_id'],
                generation=row['generation'], outcome='completed', result={'final_response': PRIVATE})
        else:
            payload = {'title': PRIVATE} if operation == 'rename' else {'sessions': [
                {'id': 'import-public', 'source': 'cli', 'messages': [{'role': 'user', 'content': PRIVATE}]},
                {'id': PRIVATE if private_owner else 'import-second', 'source': 'cli', 'messages': []}]}
            call = lambda: rt.mutate_runtime_session(db, epoch=epoch, principal_id='human',
                session_id='native' if operation == 'rename' else 'import-public', request_id='mutation',
                expected_revision=0, operation=operation, payload=payload)
        if private_owner:
            with pytest.raises(rt.RuntimeStoreError, match='privacy_denied'):
                call()
            assert snapshot(db) == before
            assert not any(statement.lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')) for statement in sql)
        else:
            result = call()
            assert result and snapshot(db) != before
            assert PRIVATE.encode() in disk(Path(db.db_path).parent)
    finally:
        db._conn.set_trace_callback(None)
        register_session('native', False)
        register_session(PRIVATE, False)
