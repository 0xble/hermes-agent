"""Large worker snapshots stay durable without poisoning an otherwise healthy turn."""
import copy
import json
import random
import string

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError


@pytest.mark.parametrize('operation', ['transcript.append', 'compression.archive'])
def test_large_operation_reopens_and_replays_exactly_once(tmp_path, operation):
    from hermes_state import SessionDB
    from hermes_state_runtime import mutate_worker_execution, register_worker_execution
    from tests.fakes.runtime_owner import begin_runtime_epoch

    db = SessionDB(tmp_path / 'state.db')
    db.create_session('owned', 'cli')
    epoch = begin_runtime_epoch(db, instance_id='fixture')
    scope = dict(epoch=epoch, execution_id='worker', session_id='owned', generation=0)
    register_worker_execution(db, **scope, kind='compute', adoption_secret='test-secret')
    def owner(method, **params):
        return mutate_worker_execution(db, **params)
    store = RuntimeSessionStore(owner, scope, tmp_path / 'outbox')
    try:
        rows = [{'role': 'user', 'content': '漢' * (750_000 if operation == 'transcript.append' else 240_000)}]
        if operation == 'compression.archive':
            store.append_messages_batch('owned', rows)
        before = store.journal['next_sequence']
        seen = []
        def lost_ack(method, **params):
            seen.append(copy.deepcopy(params))
            owner(method, **params)
            raise TimeoutError('lost-ack')
        store.rpc = lost_ack
        with pytest.raises(TimeoutError, match='lost-ack'):
            if operation == 'transcript.append':
                store.append_messages_batch('owned', rows)
            else:
                store.archive_and_compact('owned', rows, held_messages=rows, held_for_cas=rows)
        assert len(json.dumps(store.journal).encode()) > store.max_bytes
        assert store.path.stat().st_size <= store.max_bytes
        with pytest.raises(WorkerPersistenceError, match='lost-ack'):
            store.queue_token_counts('owned', input_tokens=1)
        store._outbox_owner.close()  # emulate worker exit with the receipt reply lost
        replayed = []
        def replay(method, **params):
            replayed.append(copy.deepcopy(params))
            return owner(method, **params)
        store = RuntimeSessionStore(replay, scope, tmp_path / 'outbox')
        assert replayed == seen
        assert store.journal['pending'] == []
        assert store.journal['next_sequence'] == before + 1
        active = db.get_messages_as_conversation('owned')
        assert [row['content'] for row in active] == [rows[0]['content']]
        assert db._read_one('SELECT COUNT(*) FROM worker_receipts WHERE sequence=?', (before,))[0] == 1
        store.queue_token_counts('owned', input_tokens=1)
        assert store.flush_token_counts()
    finally:
        store._outbox_owner.close()
        db.close()


@pytest.mark.parametrize('limit,size', [(500, 4000), (4 * 1024 * 1024, 4 * 1024 * 1024)])
def test_definitely_unsent_capacity_rejection_keeps_sequence_and_turn_usable(tmp_path, limit, size):
    calls = []
    def owner(method, **params):
        calls.append(params)
        return {'value': True}
    store = RuntimeSessionStore(owner, {'session_id': 's'}, tmp_path / 'outbox', max_bytes=limit)
    try:
        before = store.path.read_bytes()
        rng = random.Random(0)
        content = ''.join(rng.choices(string.ascii_letters + string.digits, k=size))
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            store.append_messages_batch('s', [{'role': 'user', 'content': content}])
        assert calls == [] and store.path.read_bytes() == before
        assert store.journal['next_sequence'] == 1 and store.journal['pending'] == []
        assert store.failure is None
        store.queue_token_counts('s', input_tokens=1)
        assert store.refresh_session_turn_lease('s', 'holder')
        assert [call['sequence'] for call in calls] == [1, 2]
        assert store.flush_token_counts()
    finally:
        store._outbox_owner.close()
