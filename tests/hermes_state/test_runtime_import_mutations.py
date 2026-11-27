"""Import rows and retry receipt either commit together or not at all."""
from tests.fakes.runtime_owner import begin_runtime_epoch as _begin_owned_runtime_epoch
import sqlite3
import pytest
from hermes_state import SessionDB
import hermes_state_runtime as rt


def test_import_rolls_back_rows_with_receipt_failure(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        epoch = _begin_owned_runtime_epoch(db, instance_id='owner')
        args = dict(epoch=epoch, principal_id='human', session_id='first', request_id='import',
                    expected_revision=0, operation='import', payload={'sessions': [
                        {'id': 'first', 'source': 'cli', 'messages': [{'role': 'user', 'content': 'keep'}]},
                        {'id': 'second', 'source': 'cli', 'parent_session_id': 'first', 'messages': []}]})
        db._execute_write(lambda c: c.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON state_meta WHEN NEW.key LIKE 'gateway.mutation.%' BEGIN SELECT RAISE(ABORT, 'receipt failed'); END"))
        with pytest.raises(sqlite3.IntegrityError, match='receipt failed'):
            rt.mutate_runtime_session(db, **args)
        assert db.get_session('first') is None and db.get_session('second') is None
        db._execute_write(lambda c: c.execute('DROP TRIGGER fail_receipt'))
        result = rt.mutate_runtime_session(db, **args)
        assert result['imported_ids'] == ['first', 'second']
        assert db.get_session('second')['parent_session_id'] == 'first'
        assert db.get_messages('first')[0]['content'] == 'keep'
        assert rt.mutate_runtime_session(db, **args) == result
        with pytest.raises(rt.RuntimeStoreError, match='admission_conflict'):
            rt.mutate_runtime_session(db, **(args | {'payload': {'sessions': []}}))
        with pytest.raises(rt.RuntimeStoreError, match='admission_conflict'):
            rt.mutate_runtime_session(db, **(args | {'payload': {'sessions': [
                {'id': 'first', 'source': 'cli', 'messages': []},
                {'id': 'invalid-member', 'messages': 'not a message list'},
            ]}}))


def test_import_never_overwrites_existing_live_rows(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('live', source='test')
        epoch = _begin_owned_runtime_epoch(db, instance_id='owner')
        rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='live', request_id='queued', payload={'text': 'keep'})
        before = db.get_session('live')
        result = rt.mutate_runtime_session(db, epoch=epoch, principal_id='human', session_id='new',
            request_id='import', expected_revision=0, operation='import', payload={'sessions': [
                {'id': 'new', 'source': 'cli', 'messages': []},
                {'id': 'live', 'source': 'cli', 'title': 'Overwrite', 'messages': []}]})
        assert result['skipped_ids'] == ['live']
        assert db.get_session('live') == before
        assert rt.list_session_admissions(db, session_id='live')[0]['status'] == 'queued'


@pytest.mark.parametrize('private_secondary', [False, True])
@pytest.mark.parametrize('secondary', ['secondary', 'secondary-' + 'x' * 1024])
def test_import_handler_validates_public_errors_but_private_members_refuse_before_sql(tmp_path, private_secondary, secondary):
    from agent.incognito import register_session

    with SessionDB(db_path=tmp_path / 'state.db') as db:
        epoch = _begin_owned_runtime_epoch(db, instance_id='owner')
        args = dict(epoch=epoch, principal_id='human', session_id='first', request_id='import',
                    expected_revision=0, operation='import', payload={'sessions': [
                        {'id': 'first', 'source': 'cli', 'messages': []},
                        {'id': secondary, 'source': 'cli', 'messages': []},
                        {'id': 'invalid-member', 'messages': 'not a message list'},
                    ]})
        before = tuple(db._conn.iterdump())
        sql = []
        register_session(secondary, private_secondary)
        try:
            db._conn.set_trace_callback(sql.append)
            reason = 'privacy_denied' if private_secondary else 'invalid_params'
            with pytest.raises(rt.RuntimeStoreError, match=f'^{reason}$'):
                rt.mutate_runtime_session(db, **args)
        finally:
            db._conn.set_trace_callback(None)
            register_session(secondary, False)
        if private_secondary:
            assert sql == []
        assert tuple(db._conn.iterdump()) == before
        # A rejected batch reserved neither rows nor its request identity.
        args['payload']['sessions'].pop()
        result = rt.mutate_runtime_session(db, **args)
        assert result['imported_ids'] == ['first', secondary]
        assert rt.mutate_runtime_session(db, **args) == result
