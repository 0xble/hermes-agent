"""A released owner finishes its FIFO and trusted notices, without reopening ingress."""

import asyncio
from pathlib import Path

import pytest

from gateway import zero_cut_handover_drain as drain
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from tests.gateway.test_wake_during_restart_drain_authority import (
    Hold, admissions, authority_route, start_human_turn,
)
from tests.waiting import async_wait_until


async def owned_route(monkeypatch, executed, hold=None):
    runner, adapter, authority, source = await authority_route(monkeypatch, executed, hold)
    turn = runner._handle_message_with_agent

    async def finite_model_turn(event, source, quick_key, run_generation):
        try:
            return await turn(event, source, quick_key, run_generation)
        finally:
            # The model-boundary fake must release the slot like the real turn finalizer.
            runner._release_running_agent_state(quick_key, run_generation=run_generation)

    monkeypatch.setattr(runner, '_handle_message_with_agent', finite_model_turn)
    return runner, adapter, authority, source


def release_handover(runner):
    runner._draining = True
    runner._external_drain_active = True
    runner._zero_cut_handover_state = {
        'accepted': True, 'updates_settled': True, 'token_lock_released': True,
        'active_turns': 0, 'idle': False, 'failed': None,
    }


def notice(runner, source, kind):
    route = runner.session_store._generate_session_key(source)
    return {
        'type': kind, 'session_id': 'owned-process', 'started_at': 1.0,
        'delegation_id': 'owned-delegation', 'event_id': 'owned-event',
        'session_key': route, 'parent_session_id': runner.session_store.peek_session_id(route),
        'platform': 'discord', 'chat_id': '42', 'chat_type': 'dm',
        'exit_code': 0, 'output': 'RESULT', 'pattern': 'RESULT',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['completion', 'async_delegation', 'delegation_auto_resume'])
async def test_released_owner_delivers_followup_and_result_once(monkeypatch, kind):
    from gateway.session_ingress_context import native_callback
    from gateway.wake import WakeNotAccepted, deliver_wake

    executed, sent, hold = [], [], Hold()
    runner, adapter, authority, source = await owned_route(monkeypatch, executed, hold)
    sending, release_send = asyncio.Event(), asyncio.Event()

    async def send(chat_id, content, reply_to=None, metadata=None):
        if 'RESULT' in content:
            sending.set()
            await release_send.wait()
        sent.append(content)
        return SendResult(success=True, message_id=str(len(sent)))

    monkeypatch.setattr(adapter, 'send', send)
    await start_human_turn(runner, adapter, authority, source, hold)
    from gateway.session_identity import replace_source
    followup = MessageEvent(text='B', source=replace_source(source, message_id='B'), message_id='B')
    with native_callback(runner, followup, Path(authority.db.db_path).parent):
        await authority.admit_native(followup)
    release_handover(runner)
    evt = notice(runner, source, kind)
    try:
        assert drain.status(runner)['idle'] is False
        with pytest.raises(WakeNotAccepted) as refused:
            await deliver_wake(adapter, text='EXTERNAL', source=source)
        assert refused.value.reason == 'runtime_draining'
        accepted = await runner._inject_watch_notification('RESULT', evt)
        assert accepted is True, 'the released owner must admit its own stable result'
        assert await runner._inject_watch_notification('RESULT', evt) is True
        hold.finish.set()
        await asyncio.wait_for(sending.wait(), 10)
        assert executed[:2] == ['A', 'B']
        assert len(executed) == 3 and 'RESULT' in executed[-1]
        assert drain.status(runner)['idle'] is False, 'a terminal row still owes its send'
        release_send.set()
        await async_wait_until(lambda: drain.status(runner)['idle'], what='owned sends and FIFO to settle')
        assert admissions(runner, authority, source) == [
            ('messaging', 'terminal'), ('messaging', 'terminal'), ('automation', 'terminal')]
        assert len([content for content in sent if 'RESULT' in content]) == 1
        assert len([content for content in sent if content == 'reply B']) == 1
        assert await runner._inject_watch_notification('RESULT', evt) is True
        await async_wait_until(lambda: drain.status(runner)['idle'], what='duplicate receipt to settle')
        assert len(executed) == 3
    finally:
        hold.finish.set()
        release_send.set()
        await asyncio.gather(*(live.task for live in authority.sessions.values() if live.task),
                             return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['watch_match', 'watch_disabled', 'watch_overflow_tripped',
                                  'watch_overflow_released'])
async def test_advisory_watch_queue_settles_during_handover(monkeypatch, kind):
    import queue
    from tools.process_registry import process_registry

    executed = []
    runner, adapter, authority, source = await owned_route(monkeypatch, executed)
    await start_human_turn(runner, adapter, authority, source, None)
    await asyncio.gather(*(live.task for live in authority.sessions.values() if live.task))
    await async_wait_until(lambda: not adapter._active_sessions, what="initial adapter send to settle")
    release_handover(runner)
    events = queue.Queue()
    evt = notice(runner, source, kind)
    if kind.startswith('watch_overflow_'):
        # Registry-wide overflow summaries have no conversation to admit into.
        evt = {'type': kind, 'message': 'watch delivery resumed'}
    events.put(evt)
    monkeypatch.setattr(process_registry, 'completion_queue', events)
    assert drain.status(runner)['idle'] is False
    await runner._drain_watch_notifications(events)
    assert events.empty(), 'advisory notices must route or explicitly settle, never livelock'
    await async_wait_until(lambda: drain.status(runner)['idle'], what='watch notice to settle')
    assert len(executed) == (1 if kind.startswith('watch_overflow_') else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('closed', ['unreleased', 'failed', '_zero_cut_stop_task', '_stop_task', 'ordinary'])
async def test_owned_allowance_closes_with_the_handover(monkeypatch, closed):
    from hermes_state_runtime import RuntimeStoreError

    executed = []
    runner, adapter, authority, source = await owned_route(monkeypatch, executed)
    await start_human_turn(runner, adapter, authority, source, None)
    await asyncio.gather(*(live.task for live in authority.sessions.values() if live.task))
    await async_wait_until(lambda: not adapter._active_sessions, what="initial adapter send to settle")
    release_handover(runner)
    if closed == 'unreleased':
        runner._zero_cut_handover_state['token_lock_released'] = False
    elif closed == 'failed':
        runner._zero_cut_handover_state['failed'] = 'transport_failure'
    elif closed == 'ordinary':
        runner._zero_cut_handover_state = None
    else:
        setattr(runner, closed, asyncio.current_task())
    assert await runner._inject_watch_notification('RESULT', notice(runner, source, 'completion')) is False
    assert executed == ['A']
    assert admissions(runner, authority, source) == [('messaging', 'terminal')]
    with pytest.raises(RuntimeStoreError) as refused:
        authority._require_admission_open(owned_work=True)
    assert refused.value.reason == 'runtime_draining'


@pytest.mark.asyncio
async def test_untrusted_automation_cannot_borrow_owned_allowance(monkeypatch):
    from hermes_state_runtime import RuntimeStoreError

    executed = []
    runner, adapter, authority, source = await owned_route(monkeypatch, executed)
    await start_human_turn(runner, adapter, authority, source, None)
    await asyncio.gather(*(live.task for live in authority.sessions.values() if live.task))
    await async_wait_until(lambda: not adapter._active_sessions, what="initial adapter send to settle")
    release_handover(runner)
    with pytest.raises(RuntimeStoreError) as refused:
        await authority.admit_automation(adapter, MessageEvent(text='EXTERNAL', source=source), 'external')
    assert refused.value.reason == 'runtime_draining'
    assert executed == ['A']


@pytest.mark.asyncio
async def test_watch_notice_remains_owned_while_route_resolution_yields(monkeypatch):
    import queue
    import threading
    from tools.process_registry import process_registry

    executed = []
    runner, adapter, authority, source = await owned_route(monkeypatch, executed)
    await start_human_turn(runner, adapter, authority, source, None)
    await asyncio.gather(*(live.task for live in authority.sessions.values() if live.task))
    await async_wait_until(lambda: not adapter._active_sessions, what='initial send to settle')
    release_handover(runner)
    events = queue.Queue()
    events.put(notice(runner, source, 'watch_match'))
    monkeypatch.setattr(process_registry, 'completion_queue', events)
    resolving, release = threading.Event(), threading.Event()
    build_source = runner._build_process_event_source

    def resolve_after_barrier(evt):
        resolving.set()
        assert release.wait(10), 'test must release route resolution'
        return build_source(evt)

    monkeypatch.setattr(runner, '_build_process_event_source', resolve_after_barrier)
    draining = asyncio.create_task(runner._drain_watch_notifications(events))
    try:
        assert await asyncio.to_thread(resolving.wait, 10)
        assert events.empty()
        assert drain.status(runner)['idle'] is False, 'a detached watch event still owns its admission'
    finally:
        release.set()
        await draining
    await async_wait_until(lambda: drain.status(runner)['idle'], what='watch admission and send to settle')
    assert len(executed) == 2 and 'RESULT' in executed[-1]
