"""Worker receipts retain rejected occurrences without changing model replay."""
import os

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from hermes_state import SessionDB
from hermes_state_errors import CompressionSessionClosedError, SessionTurnLeaseLostError
from hermes_state_runtime import (
    RuntimeStoreError, begin_runtime_epoch, mutate_worker_execution, register_worker_execution,
)


@pytest.fixture
def owner(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', 'cli')
    db.create_session('foreign', 'cli')
    epoch = begin_runtime_epoch(db, instance_id='owner')
    scope = dict(epoch=epoch, execution_id='worker', session_id='s', generation=0)
    register_worker_execution(db, **scope, kind='compute', adoption_secret='fixture')
    yield db, scope
    db.close()


def test_worker_archives_each_occurrence_and_preserves_metadata(owner, tmp_path):
    db, scope = owner
    db.append_message('s', 'user', 'previous ask')
    db.append_message('s', 'assistant', 'previous answer')
    before = db.get_messages_as_conversation('s')
    store = RuntimeSessionStore(lambda method, **p: mutate_worker_execution(db, **p), scope, tmp_path / 'outbox')
    try:
        with pytest.raises(WorkerPersistenceError, match='permission_denied'):
            store.append_message('foreign', 'user', 'not permitted', archived=True)
        # Display history intentionally folds identical content/timestamp copies.
        # Separate rejected occurrences preserve their own original timestamps.
        stamps = (1700000000.5, 1700000001.5)
        ids = [store.append_message(
            's', 'user', 'rejected input', archived=True, message_uid=uid * 32,
            timestamp=stamp, platform_message_id='platform-123',
            display_kind='text', display_metadata={'source': 'fixture'},
        ) for uid, stamp in zip(('a', 'b'), stamps, strict=True)]
        assert ids[0] != ids[1]
        assert db.get_messages_as_conversation('s') == before
        rows = db.get_messages('s', include_compacted=True)
        assert [row['content'] for row in rows] == ['previous ask', 'previous answer', 'rejected input', 'rejected input']
        for row, uid, row_id, stamp in zip(rows[-2:], ('a', 'b'), ids, stamps, strict=True):
            assert row['id'] == row_id
            assert row['message_uid'] == uid * 32
            assert row['timestamp'] == stamp
            assert row['platform_message_id'] == 'platform-123'
            assert row['display_kind'] == 'text'
            assert row['display_metadata'] == {'source': 'fixture'}
            assert tuple(db._conn.execute('SELECT active, compacted FROM messages WHERE id=?', (row_id,)).fetchone()) == (0, 1)
        assert db.get_resume_conversations('s')[1][-1]['content'] == 'rejected input'
        live_id = store.append_message('s', 'user', 'next valid input')
        assert db.get_messages_as_conversation('s')[-1]['content'] == 'next valid input'
        assert live_id > ids[-1]
        assert db.get_messages('foreign', include_compacted=True) == []
    finally:
        store.close()


def test_single_message_receipts_replay_atomically_and_keep_writer_guards(owner):
    db, scope = owner
    payload = {'role': 'user', 'content': 'rejected input', 'archived': True}

    def apply(sequence, body=payload, **overrides):
        return mutate_worker_execution(db, **(scope | overrides), sequence=sequence,
                                       operation='transcript.append_one', payload=body)

    # Ordinary inserts remain allowed while a compressor holds a watermark lock.
    compressor = f'pid={os.getpid()}:compression=fixture'
    assert db.try_acquire_compression_lock('s', compressor)
    receipt = apply(1)
    db.release_compression_lock('s', compressor)
    revision = db.get_session('s')['runtime_revision']
    assert apply(1) == receipt
    assert db.get_session('s')['runtime_revision'] == revision
    with pytest.raises(RuntimeStoreError, match='admission_conflict'):
        apply(1, payload | {'content': 'different'})
    with pytest.raises(RuntimeStoreError, match='permission_denied'):
        apply(2, session_id='foreign')
    with pytest.raises(RuntimeStoreError, match='stale_generation'):
        apply(2, generation=1)
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        apply(3)
    for invalid in ({'sql': 'DELETE FROM messages'}, {'archived': 'yes'}, {'_row_id': receipt['row_id']}):
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            apply(2, payload | invalid)
    stale = f'pid={os.getpid()}:turn=stale'
    current = f'pid={os.getpid()}:turn=current'
    assert db.try_acquire_session_turn_lease('s', current)
    with pytest.raises(SessionTurnLeaseLostError):
        apply(2, payload | {'turn_lease_holder': stale})
    db.release_session_turn_lease('s', current)
    db.end_session('s', 'compression')
    with pytest.raises(CompressionSessionClosedError):
        apply(2)
    assert len(db.get_messages('s', include_compacted=True)) == 1
    assert db._conn.execute('SELECT last_sequence FROM worker_executions WHERE execution_id=?', ('worker',)).fetchone()[0] == 1
    begin_runtime_epoch(db, instance_id='new-owner')
    with pytest.raises(RuntimeStoreError, match='stale_epoch'):
        apply(2)
