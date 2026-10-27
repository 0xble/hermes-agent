"""Real producer events cross automation admission, including retry and boot notices."""
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import MessageEvent
from gateway.run_notifications import _watch_notification_metadata
from gateway.session_automation import admit_process_notice, producer_identity
from gateway.session_envelope import restore_native
from hermes_state_runtime import RuntimeStoreError, list_session_admissions
from tests.gateway.test_automation_notification_category import _native_owner
from tests.gateway.test_delegation_retry_delivery import _failed_row


def _rows(authority, sid):
    return list_session_admissions(authority.db, session_id=sid, pending_only=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['watch_match', 'watch_disabled', 'completion', 'heartbeat', 'async_delegation'])
async def test_registry_producers_cross_real_admission(tmp_path, monkeypatch, kind):
    from tools import async_delegation as ad
    from tools import process_registry as pr
    runner, authority, adapter, source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', registry)
    process = pr.ProcessSession(id='proc-test', command='verify', task_id='task',
        session_key=metadata['gateway_session_key'], parent_session_id=metadata['gateway_session_id'])
    try:
        if kind == 'watch_match':
            process.watch_patterns = ['READY']
            registry._check_watch_patterns(process, 'READY\n')
        elif kind == 'watch_disabled':
            registry._emit_watch_disabled(process, 3, 'rate limited')
        elif kind == 'heartbeat':
            registry._running[process.id] = process
            process.output_buffer = 'progress\n'
            process.total_output_chars = len(process.output_buffer)
            registry._emit_heartbeat(process, process.started_at + 60)
        elif kind == 'completion':
            process.notify_on_complete = True
            process.exited, process.exit_code = True, 0
            registry._publish_finished(process, True)
        else:
            monkeypatch.setattr(ad, '_db_path', lambda: tmp_path / 'delegations.db')
            record = dict(delegation_id='deleg-result', goal='verify', goals=['verify'],
                is_batch=False, session_key=metadata['gateway_session_key'],
                parent_session_id=metadata['gateway_session_id'], dispatched_at=1.0,
                completed_at=2.0, _durable_state='queued')
            ad._persist_dispatch(record)
            ad._push_completion_event(record, {'summary': 'verified'}, 'completed')
        evt = registry.completion_queue.get_nowait()
        assert evt['type'] == kind
        # Per-process watchers attach their captured parent pin before injection.
        evt.setdefault('parent_session_id', process.parent_session_id)
        from tools.process_registry_notifications import format_process_notification
        text = format_process_notification(evt)
        assert text and not text.startswith('/')
        assert await runner._inject_watch_notification(text, evt) is True
        admitted = _rows(authority, metadata['gateway_session_id'])[-1]
        assert admitted['request_id'] == producer_identity(runner, evt)
        assert restore_native(admitted['payload'], runner).internal
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_retry_producer_commits_claim_and_cancel_fence_survives_restore(tmp_path, monkeypatch):
    from tools import async_delegation as ad, delegation_resume as dr
    from hermes_cli.session_controls import RETRY_WAKE_METADATA_KEY, internal_wake_is_current
    runner, authority, adapter, source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
    monkeypatch.setattr(ad, '_db_path', lambda: tmp_path / 'delegations.db')
    _failed_row('deleg-retry', 'rate_limit')
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute('UPDATE async_delegations SET origin_session=?, parent_session_id=? WHERE delegation_id=?',
                     (metadata['gateway_session_key'], metadata['gateway_session_id'], 'deleg-retry'))
    dr.schedule_retry('deleg-retry', now=1_800_000_060.0)
    due = dr.retry_status('deleg-retry')['retry_due_at']
    (action,) = dr.sweep_retries(now=due + 1)
    monkeypatch.setattr(runner, '_classify_completion_target', AsyncMock(return_value='deliver'))
    monkeypatch.setattr(runner, '_authorized_completion_notice_target', AsyncMock(return_value=(source, True)))
    try:
        await runner._deliver_delegation_retry_action(action)
        assert dr.retry_status('deleg-retry')['retry_state'] == 'notified'
        admitted = _rows(authority, metadata['gateway_session_id'])[-1]
        restored = restore_native(admitted['payload'], runner)
        assert restored.metadata[RETRY_WAKE_METADATA_KEY] == {
            'delegation_id': 'deleg-retry', 'claim': action['claim']}
        assert internal_wake_is_current(restored)
        assert dr.cancel_pending_retries(delegation_id='deleg-retry') == {'deleg-retry'}
        assert not internal_wake_is_current(restored)
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_boot_notice_producer_has_its_own_replay_identity(tmp_path, monkeypatch):
    from tools import delegation_resume as dr, process_registry as pr
    runner, authority, adapter, source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', registry)
    record = dict(delegation_id='deleg-boot', session_key=metadata['gateway_session_key'],
                  parent_session_id=metadata['gateway_session_id'], task={})
    monkeypatch.setattr(dr, 'list_boot_candidates', lambda: [record])
    try:
        assert runner._schedule_auto_resume_delegations() == 1
        evt = registry.completion_queue.get_nowait()
        assert evt['type'] == 'delegation_auto_resume'
        assert await runner._inject_watch_notification(evt['text'], evt) is True
        assert await runner._inject_watch_notification(evt['text'], evt) is True
        admitted = _rows(authority, metadata['gateway_session_id'])
        assert len(admitted) == 2  # human seed and one boot notice
        assert admitted[-1]['request_id'] != producer_identity(runner, {**evt, 'type': 'async_delegation'})
    finally:
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stamp', [None, {}, {'delegation_id': 'd'},
    {'delegation_id': 'd', 'claim': ''}, {'delegation_id': [], 'claim': 'c'},
    {'delegation_id': 'd', 'claim': 'c', 'foreign': True}])
async def test_retry_stamp_is_closed_and_validated(tmp_path, monkeypatch, stamp):
    runner, authority, adapter, source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
    event = MessageEvent(text='retry', source=source, internal=True,
                         metadata={**metadata, 'delegation_retry_notice': stamp})
    try:
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            await authority.admit_automation(adapter, event, 'retry')
        assert len(_rows(authority, metadata['gateway_session_id'])) == 1
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_real_process_metadata_still_rejects_unknown_key(tmp_path, monkeypatch):
    runner, authority, adapter, source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
    evt = dict(type='watch_match', session_id='proc', event_id='event',
               session_key=metadata['gateway_session_key'], parent_session_id=metadata['gateway_session_id'])
    event = MessageEvent(text='result', source=source, internal=True,
                         metadata={**_watch_notification_metadata(evt, source), 'foreign': True})
    try:
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            await admit_process_notice(runner, authority, adapter, event, evt)
        assert len(_rows(authority, metadata['gateway_session_id'])) == 1
    finally:
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('local', [False, True])
async def test_valid_retry_stamp_crosses_snapshot_and_restore(tmp_path, monkeypatch, local):
    from gateway.session_automation import restore_local_automation
    from tests.gateway.test_automation_notification_category import _authority
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from gateway.session_local_title import title_new_session
    stamp = {'delegation_id': 'deleg-retry', 'claim': 'retry:fixture'}
    if local:
        runner, authority = await _authority(tmp_path, monkeypatch)
        actor = Principal('owner', 'default', frozenset({'session:create', 'session:submit', 'session:read'}), 'fixture')
        ref = create_local_session(authority, actor, dict(request_id='local', source='gui', model='fixture', toolsets=[]))
        live = authority.sessions[ref.session_id]
        title_new_session(authority, ref, 'local')
        source = live.source
        adapter = runner._adapter_for_source(source)
        metadata = {'gateway_session_key': live.route, 'gateway_session_id': ref.session_id}
    else:
        runner, authority, adapter, source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
    event = MessageEvent(text='retry notice', source=source, internal=True,
                         metadata={**metadata, 'delegation_retry_notice': stamp})
    try:
        await authority.admit_automation(adapter, event, 'retry-fixture')
        row = _rows(authority, metadata['gateway_session_id'])[-1]
        restored = restore_local_automation(authority, ref, row) if local else restore_native(row['payload'], runner)
        assert restored.metadata['delegation_retry_notice'] == stamp
    finally:
        authority.db.close()


def test_notice_identity_is_fail_closed_and_claim_scoped():
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    first = {'type': 'delegation_retry', 'delegation_id': 'deleg', 'retry_notice_claim': 'retry:first'}
    second = {**first, 'retry_notice_claim': 'retry:second'}
    assert producer_identity(runner, first) != producer_identity(runner, second)
    for evt in ({'type': 'delegation_retry', 'delegation_id': 'deleg'},
                {'type': 'delegation_auto_resume'}, {'type': 'foreign'}):
        with pytest.raises(RuntimeStoreError, match='invalid_params') as refused:
            producer_identity(runner, evt)
        assert refused.value.field == 'producer_identity'
