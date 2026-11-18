"""Mandatory worker writes must not report success after a rejected transcript row."""
import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from agent.session_persistence import SessionPersistenceMixin
from agent.context_compressor import _DB_PERSISTED_MARKER


@pytest.mark.parametrize('role', ['user', 'assistant', 'tool'])
@pytest.mark.parametrize('boundary', ['persist_session', 'managed_worker_flush'])
def test_oversized_transcript_fails_the_turn(tmp_path, role, boundary):
    calls = []
    store = RuntimeSessionStore(lambda method, **params: calls.append(params),
                                {'session_id': 's'}, tmp_path / 'outbox', max_bytes=1000)
    agent = SessionPersistenceMixin()
    agent._session_db = store
    agent.session_id = 's'
    agent._session_db_created = True
    messages = [{'role': role, 'content': 'x' * 4000}]
    before = store.path.read_bytes()
    try:
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            if boundary == 'persist_session':
                agent._persist_session(messages)
            else:
                # Final text/tool-progress paths swallow append errors. The managed
                # worker's mandatory flush before sending its result must catch them.
                assert agent._flush_messages_to_session_db(messages) is False
                store.flush_token_counts()
                store.finish()
        assert store.failure == 'outbox_full'
        assert calls == [] and store.path.read_bytes() == before
        assert store.journal['next_sequence'] == 1 and store.journal['pending'] == []
        assert not messages[0].get(_DB_PERSISTED_MARKER)
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            store.finish()
    finally:
        store._outbox_owner.close()


@pytest.mark.parametrize('operation', ['archive', 'usage', 'lease', 'finish'])
def test_other_mandatory_capacity_rejections_are_sticky(tmp_path, operation):
    calls = []
    store = RuntimeSessionStore(lambda method, **params: calls.append(params),
                                {'session_id': 's'}, tmp_path / 'outbox', max_bytes=500)
    before = store.path.read_bytes()
    try:
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            if operation == 'archive':
                store.archive_and_compact('s', [{'role': 'assistant', 'content': 'x' * 4000}])
            elif operation == 'usage':
                store.queue_token_counts('s', model='x' * 4000)
            elif operation == 'lease':
                store.refresh_session_turn_lease('s', 'x' * 4000)
            else:
                store.max_bytes = 80
                store.finish()
        assert calls == [] and store.path.read_bytes() == before
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            store.flush_token_counts()
    finally:
        store._outbox_owner.close()
