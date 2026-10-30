"""Receipt-time activity across the real adapter, shared busy burst and durable FIFO."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from gateway.platforms.event import MessageEvent
from gateway.session_ingress_burst import burst_gate, holds_busy_burst
from tests.gateway.test_busy_burst_serial_reader import _busy_owner, _rows, _signal


async def flush_busy_burst(adapter, key):
    if burst := getattr(adapter, '_shared_bursts', {}).get(key):
        await burst_gate(adapter, MessageEvent(text='/status', source=burst.head.source), key)


@pytest_asyncio.fixture
async def shared_busy(monkeypatch):
    monkeypatch.setattr('gateway.run._load_gateway_config', lambda: {
        'model': {'default': 'ordinary', 'provider': 'openrouter', 'idle_revert_minutes': 30,
                  'attended': {'model': 'attended', 'provider': 'openrouter'}},
    })
    adapter, source = _signal(monkeypatch)
    owner = await _busy_owner(monkeypatch, adapter, source, busy_text_mode='queue',
                              busy_text_timing=(60, 60))
    runner = owner.runner
    schedule = Mock(wraps=runner._schedule_attended_user_activity)
    persist = AsyncMock(wraps=runner.async_session_store.mark_user_activity)
    monkeypatch.setattr(runner, '_schedule_attended_user_activity', schedule)
    monkeypatch.setattr(runner.async_session_store, 'mark_user_activity', persist)
    # A clock scoped to receipt logging, not asyncio's timers or SQLite's clocks.
    clock = [1800000000.0]
    monkeypatch.setattr('gateway.reply_latency.time', SimpleNamespace(time=lambda: clock[0]))
    drained = []
    turn = runner._handle_message_with_agent

    async def observe(event, *args):
        drained.append(event)
        return await turn(event, *args)

    monkeypatch.setattr(runner, '_handle_message_with_agent', observe)
    assert runner.session_authority is not None
    assert owner.key in adapter._active_sessions
    env = SimpleNamespace(adapter=adapter, source=source, owner=owner, runner=runner,
                          clock=clock, schedule=schedule, persist=persist, drained=drained,
                          settled=False)
    yield env
    if not env.settled:
        await flush_busy_burst(adapter, owner.key)
        await owner.settle(1)
    pending = tuple(adapter._background_tasks)
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=10)


@pytest.mark.asyncio
async def test_active_shared_input_stamps_before_burst_commit(shared_busy):
    env = shared_busy
    event = MessageEvent(text='human follow-up', source=env.source, message_id='busy')
    assert env.adapter._shared_runtime_owns_input(event)
    await env.adapter.handle_message(event)
    env.schedule.assert_called_once_with(event, event.source, env.owner.key)
    assert event._attended_activity_received_at == env.clock[0]
    assert holds_busy_burst(env.adapter, env.owner.key)
    await event._attended_activity_task
    assert event._attended_activity_marked
    env.persist.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', [
    'internal', 'relay', 'goal_wakeup', 'process_notification', 'bot', 'bot_refused',
    'unauthorized', 'unauthorized_no_control',
])
async def test_nonhuman_or_refused_shared_input_never_persists_activity(
        shared_busy, monkeypatch, caplog, kind):
    env = shared_busy
    source = replace(env.source)
    event = MessageEvent(text='follow-up', source=source, message_id=kind)
    if kind == 'internal':
        event.internal = True
    elif kind == 'relay':
        event.text = '[System: Relay]\nAutomated update'
    elif kind in {'goal_wakeup', 'process_notification'}:
        event.metadata = {'kind': kind}
    elif kind in {'bot', 'bot_refused'}:
        source.is_bot = True
        monkeypatch.setattr(env.runner, '_is_user_authorized_for_source', lambda source: True)
        monkeypatch.setattr(env.runner, '_admit_bot_message_for_source', lambda source: kind != 'bot_refused')
    elif kind.startswith('unauthorized'):
        source.user_id = 'unauthorized-user'
        source.user_name = 'Unauthorized'
        event.allow_gateway_control = kind != 'unauthorized_no_control'
    await env.adapter.handle_message(event)
    assert getattr(event, '_attended_activity_task', None) is None
    assert getattr(event, '_attended_activity_received_at', None) is None
    await flush_busy_burst(env.adapter, env.owner.key)
    env.persist.assert_not_awaited()
    if kind.startswith('unauthorized') or kind == 'bot_refused':
        env.schedule.assert_not_called()
        assert not getattr(event, '_gateway_accepted', False)
        assert not any(row['status'] == 'queued' for row in _rows(env.owner.authority))
    if kind.startswith('unauthorized'):
        assert (
            'Dropping message from unauthorized user in active session: '
            f'user=unauthorized-user (Unauthorized), platform=signal, session={env.owner.key}'
        ) in caplog.text
        assert event.text not in caplog.text
    else:
        assert 'Dropping message from unauthorized user' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('user_id', [None, 'unauthorized-user'])
@pytest.mark.parametrize('allow_gateway_control', [False, True])
async def test_internal_busy_input_keeps_existing_admission_without_user_auth(
        shared_busy, caplog, user_id, allow_gateway_control):
    env = shared_busy
    source = replace(env.source, user_id=user_id)
    assert not env.runner._is_user_authorized_for_source(source)
    assert env.runner._is_session_running(env.owner.key)
    assert env.runner._session_key_for_source(source) == env.owner.key
    # _busy_owner holds the real FIFO turn before model construction. Exercise the running-agent
    # branch a completion actually encounters, rather than the pre-model merge-only sentinel.
    agent = Mock()
    agent.get_activity_summary.return_value = {'idle_seconds': 0}
    env.runner._session_state(env.owner.key).turn.agent = agent
    event = MessageEvent(text='internal wake', source=source,
                         message_id='internal-wake', internal=True,
                         allow_gateway_control=allow_gateway_control)
    from gateway.wake import admit_internal_event

    await admit_internal_event(env.adapter, event)
    assert event._gateway_accepted
    assert env.adapter._pending_messages[env.owner.key] is event
    assert getattr(event, '_attended_activity_received_at', None) is None
    env.persist.assert_not_awaited()
    assert 'Dropping message from unauthorized user' not in caplog.text
    # This is the existing internal adapter FIFO, not a forged external native admission.
    env.adapter._pending_messages.pop(env.owner.key)


@pytest.mark.asyncio
async def test_each_merged_shared_receipt_survives_fifo_drain_without_restamping(shared_busy):
    env = shared_busy
    first = MessageEvent(text='first', source=env.source, message_id='first')
    second = MessageEvent(text='second', source=env.source, message_id='second')
    await env.adapter.handle_message(first)
    first_stamp = env.clock[0]
    env.clock[0] += 5
    await env.adapter.handle_message(second)
    second_stamp = env.clock[0]
    assert env.schedule.call_count == 2
    assert first._attended_activity_received_at == first_stamp
    assert second._attended_activity_received_at == second_stamp
    await asyncio.gather(first._attended_activity_task, second._attended_activity_task)
    assert env.persist.await_count == 2
    assert sorted(call.kwargs['when'].timestamp() for call in env.persist.await_args_list) == [first_stamp, second_stamp]
    env.clock[0] += 3600
    await flush_busy_burst(env.adapter, env.owner.key)
    await env.owner.settle(2)
    env.settled = True
    assert len(env.drained) == 1
    replay = env.drained[0]
    assert replay.text == 'first\nsecond'
    assert replay._attended_activity_received_at == first_stamp
    assert replay._attended_activity_marked
    assert env.persist.await_count == 2
    assert env.runner._session_state(env.owner.key).conversation.last_user_activity_at == second_stamp


@pytest.mark.asyncio
async def test_unauthorized_followup_cannot_merge_into_authorized_head(shared_busy, monkeypatch):
    env = shared_busy
    head = MessageEvent(text='allowed', source=env.source, message_id='allowed')
    await env.adapter.handle_message(head)
    await head._attended_activity_task
    monkeypatch.setattr(env.runner, '_is_user_authorized_for_source', lambda source: False)
    refused = MessageEvent(text='refused', source=env.source, message_id='refused')
    await env.adapter.handle_message(refused)
    assert head.text == 'allowed'
    assert not refused._gateway_accepted
    assert getattr(refused, '_attended_activity_task', None) is None
    env.schedule.assert_called_once_with(head, head.source, env.owner.key)
    monkeypatch.setattr(env.runner, '_is_user_authorized_for_source', lambda source: True)


@pytest.mark.asyncio
async def test_busy_bot_budget_is_not_charged_again_at_fifo_commit(shared_busy, monkeypatch):
    env = shared_busy
    budget = Mock(return_value=True)
    monkeypatch.setattr(env.runner, '_admit_bot_message_for_source', budget)
    monkeypatch.setattr(env.runner, '_is_user_authorized_for_source', lambda source: True)
    event = MessageEvent(text='follow-up', source=replace(env.source, is_bot=True), message_id='budget')
    await env.adapter.handle_message(event)
    await flush_busy_burst(env.adapter, env.owner.key)
    budget.assert_called_once_with(event.source)


@pytest.mark.asyncio
async def test_native_retry_retains_original_receipt_but_changed_content_still_conflicts(shared_busy):
    from gateway.session_ingress_media import ingress_fingerprint, reconcile_native_retry
    from hermes_state_runtime import RuntimeStoreError, admit_session_input

    env = shared_busy
    event = MessageEvent(text='repeatable', source=env.source, message_id='retry')
    await env.adapter.handle_message(event)
    await event._attended_activity_task
    await flush_busy_burst(env.adapter, env.owner.key)
    row = next(row for row in _rows(env.owner.authority) if row['request_id'] == 'retry')
    original = row['payload']
    retry = deepcopy(original)
    retry['native_text_v1']['attended_activity'] = {'received_at': env.clock[0] + 3600, 'marked': False}
    assert ingress_fingerprint(retry) == ingress_fingerprint(original)

    def admit(payload):
        resolved = reconcile_native_retry(env.owner.authority.db, principal_id=row['principal_id'],
            session_id=row['target_session_id'], request_id=row['request_id'], payload=payload)
        return admit_session_input(env.owner.authority.db, epoch=env.owner.authority.epoch,
            principal_id=row['principal_id'], session_id=row['target_session_id'],
            request_id=row['request_id'], payload=resolved), resolved

    receipt, resolved = await asyncio.to_thread(admit, retry)
    assert receipt['admission_id'] == row['admission_id']
    assert resolved == retry  # reconciliation only owns media, not local receipt identity
    assert receipt['payload'] == original
    retry['text'] = 'changed'
    with pytest.raises(RuntimeStoreError, match='admission_conflict'):
        await asyncio.to_thread(admit, retry)


@pytest.mark.asyncio
async def test_failed_busy_receipt_retries_original_clock_after_fifo_restore(shared_busy):
    env = shared_busy
    env.persist.side_effect = RuntimeError('transient activity write failure')
    event = MessageEvent(text='retry original time', source=env.source, message_id='failed-stamp')
    await env.adapter.handle_message(event)
    await event._attended_activity_task
    original_stamp = event._attended_activity_received_at
    await flush_busy_burst(env.adapter, env.owner.key)
    env.persist.side_effect = None
    env.clock[0] += 3600
    await env.owner.settle(2)
    env.settled = True
    assert env.drained[0]._attended_activity_received_at == original_stamp
    env.persist.assert_awaited_with(env.owner.key, when=datetime.fromtimestamp(original_stamp))
