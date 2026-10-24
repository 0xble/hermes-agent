"""Rewind fences the generation and preserves archived history."""
import pytest
from hermes_state import SessionDB
import hermes_state_runtime as rt


def test_rewind_has_existing_semantics_with_atomic_retry(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        db.append_messages_batch('s', [{'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'reply'}, {'role': 'user', 'content': 'again'}])
        target = db.get_messages('s')[-1]['id']
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        args = dict(epoch=epoch, principal_id='human', session_id='s', request_id='rewind',
            expected_revision=0, expected_generation=0, operation='rewind', payload={'target_message_id': target})
        receipt = rt.mutate_runtime_session(db, **args)
        assert receipt['rewound_count'] == 1 and receipt['target_message']['content'] == 'again'
        assert [m['content'] for m in db.get_messages('s')] == ['first', 'reply']
        assert db.message_count('s') == 3
        assert db.get_session('s')['runtime_generation'] == 1
        db.append_messages_batch('s', [
            {'role': 'user', 'content': 'later ask'},
            {'role': 'assistant', 'content': 'later reply'},
        ])
        before = _rewind_state(db)
        assert rt.mutate_runtime_session(db, **args) == receipt
        assert _rewind_state(db) == before
        with pytest.raises(rt.RuntimeStoreError, match='revision_conflict'):
            rt.mutate_runtime_session(db, **{**args, 'request_id': 'stale-rewind'})
        assert _rewind_state(db) == before
        assert db.get_session('s')['rewind_count'] == 1
        assert [m['content'] for m in db.get_messages('s')] == [
            'first', 'reply', 'later ask', 'later reply',
        ]


def _rewind_state(db):
    return tuple(tuple(tuple(row) for row in db._read_all(sql)) for sql in (
        'SELECT * FROM messages ORDER BY id',
        'SELECT * FROM sessions ORDER BY id',
        'SELECT * FROM state_meta ORDER BY key',
    ))


@pytest.mark.parametrize('entrypoint', ['runtime', 'storage'])
@pytest.mark.parametrize('target_kind', ['archived_user', 'active_assistant'])
def test_rewind_refuses_invalid_targets_without_changing_later_work(tmp_path, entrypoint, target_kind):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        db.append_messages_batch('s', [
            {'role': 'user', 'content': 'first ask'},
            {'role': 'assistant', 'content': 'first reply'},
            {'role': 'user', 'content': 'second ask'},
            {'role': 'assistant', 'content': 'second reply'},
        ])
        archived = db.get_messages('s')[-2]['id']
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        rt.mutate_runtime_session(db, epoch=epoch, principal_id='human', session_id='s',
            request_id='first-rewind', expected_revision=0, expected_generation=0,
            operation='rewind', payload={'target_message_id': archived})
        db.append_messages_batch('s', [
            {'role': 'user', 'content': 'later ask'},
            {'role': 'assistant', 'content': 'later reply'},
        ])
        target = archived if target_kind == 'archived_user' else db.get_messages('s')[-1]['id']
        current = db.get_session('s')
        assert current is not None
        before = _rewind_state(db)
        reason = 'not active' if target_kind == 'archived_user' else "must be a 'user'"
        with pytest.raises(ValueError, match=reason):
            if entrypoint == 'storage':
                db.rewind_to_message('s', target)
            else:
                rt.mutate_runtime_session(db, epoch=epoch, principal_id='human', session_id='s',
                    request_id='fresh-rewind', expected_revision=current['runtime_revision'],
                    expected_generation=current['runtime_generation'], operation='rewind',
                    payload={'target_message_id': target})
        assert _rewind_state(db) == before
        assert [m['content'] for m in db.get_messages('s')] == [
            'first ask', 'first reply', 'later ask', 'later reply',
        ]


def test_rewind_refuses_queued_work(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        db.append_message('s', role='user', content='keep')
        target = db.get_messages('s')[0]['id']
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s', request_id='input', payload={'text': 'queued'})
        with pytest.raises(rt.RuntimeStoreError, match='session_busy'):
            rt.mutate_runtime_session(db, epoch=epoch, principal_id='human', session_id='s', request_id='rewind',
                expected_revision=0, expected_generation=0, operation='rewind', payload={'target_message_id': target})
        assert db.get_messages('s')[0]['content'] == 'keep'
