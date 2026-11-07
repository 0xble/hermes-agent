"""A slow goal judge must run after the durable answer reaches its transport."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from gateway.session_results import execution_result
from tests.fakes.runtime_owner import initialize_session_authority
from tests.gateway.test_busy_burst_serial_reader import _rows, _signal
from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter_lifecycle', [False, True])
async def test_handler_bookkeeping_follows_its_delivery_owner(monkeypatch, tmp_path, adapter_lifecycle):
    from tests.gateway.test_duplicate_user_message import _bootstrap

    adapter, source = _signal(monkeypatch)
    runner = _bootstrap(monkeypatch, tmp_path)
    runner.adapters = {source.platform: adapter}
    sends, sends_at_hook = [], []
    hooks = AsyncMock(side_effect=lambda **kwargs: sends_at_hook.append(list(sends)))

    async def turn(event, source, key, generation):
        runner._bind_adapter_run_generation(adapter, key, generation)
        return 'ok'

    async def send(chat_id, content, **kwargs):
        sends.append(content)
        return SendResult(success=True, message_id='answer')

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    monkeypatch.setattr(runner, '_run_post_turn_hooks', hooks)
    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(adapter, 'send_typing', lambda *a, **k: asyncio.sleep(0))
    event = MessageEvent(text='question', source=source, message_id='direct')

    if adapter_lifecycle:
        adapter.set_message_handler(runner._handle_message)
        await adapter._process_message_background(event, runner._session_key_for_source(source))
    else:
        assert await runner._handle_message(event) == 'ok'
    hooks.assert_awaited_once()
    assert sends_at_hook == ([['ok']] if adapter_lifecycle else [[]])
    assert not adapter._post_delivery_callbacks_by_generation


@pytest.mark.asyncio
@pytest.mark.parametrize('invalidate_at', ['sweep', 'judge_return'])
async def test_superseded_bookkeeping_cannot_judge_or_continue(monkeypatch, invalidate_at):
    adapter, source = _signal(monkeypatch)
    runner = GatewayRunner(GatewayConfig())
    runner.adapters = {source.platform: adapter}
    key = runner._session_key_for_source(source)
    generation = runner._begin_session_run_generation(key)
    cleanup, enqueue = AsyncMock(), Mock()
    manager = SimpleNamespace(
        session_id='goal-session', state=SimpleNamespace(created_at=1), is_active=lambda: True,
        evaluate_after_turn=Mock(return_value={'should_continue': True, 'continuation_prompt': 'continue'}))

    async def hooks(**kwargs):
        await runner._post_turn_goal_continuation(
            session_entry=SimpleNamespace(session_id=manager.session_id, session_key=key),
            source=source, final_response='ok', run_generation=kwargs.get('run_generation'))

    async def evaluate(fn):
        decision = fn()
        runner._invalidate_session_run_generation(key, reason='stop during judge')
        return decision

    monkeypatch.setattr(runner, '_run_post_turn_hooks', hooks)
    monkeypatch.setattr(runner, '_post_turn_manager', AsyncMock(return_value=manager))
    monkeypatch.setattr(runner, '_run_in_executor_with_context', evaluate)
    monkeypatch.setattr(runner, '_enqueue_fifo', enqueue)
    monkeypatch.setattr('gateway.wake.automatic_route_available', AsyncMock(return_value=True))
    adapter.register_post_delivery_callback(key, cleanup, generation=generation)
    token = execution_result.set({})
    try:
        await runner._defer_post_turn_hooks(
            agent_result='ok', source=source, is_internal=False,
            event=MessageEvent(text='question', source=source), session_key=key, generation=generation)
    finally:
        execution_result.reset(token)
    delivery_generation = generation
    if invalidate_at == 'sweep':
        delivery_generation = runner._invalidate_session_run_generation(key, reason='new session')
    await adapter._fire_post_delivery_callback(key, delivery_generation)

    cleanup.assert_awaited_once()
    assert manager.evaluate_after_turn.call_count == int(invalidate_at == 'judge_return')
    enqueue.assert_not_called()
    assert not adapter._post_delivery_callbacks_by_generation


@pytest.mark.asyncio
async def test_slow_post_turn_hook_does_not_hold_settlement_or_final_delivery(monkeypatch):
    adapter, source = _signal(monkeypatch)
    runner = GatewayRunner(GatewayConfig())
    adapter.gateway_runner = runner
    runner.adapters = {adapter.platform: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    entered, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    statuses_at_send = []
    messages_at_send = []

    async def turn(*args):
        sid = _rows(authority)[0]['target_session_id']
        await asyncio.to_thread(authority.db.append_message, sid, 'assistant', 'ok')
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'

    async def slow_judge(**kwargs):
        entered.set()
        await release.wait()

    async def send(chat_id, content, reply_to=None, metadata=None):
        statuses_at_send.extend(row['status'] for row in _rows(authority))
        sid = _rows(authority)[0]['target_session_id']
        messages_at_send.extend(authority.db.get_messages(sid))
        delivered.set()
        return SendResult(success=True, message_id='answer')

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    monkeypatch.setattr(runner, '_run_post_turn_hooks', slow_judge)
    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(adapter, 'send_typing', lambda *a, **k: asyncio.sleep(0))
    await adapter.handle_message(MessageEvent(text='question', source=source, message_id='inbound'))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert delivered.is_set(), 'post-turn bookkeeping blocked the final reply'
        assert statuses_at_send == ['terminal'], 'delivery preceded durable settlement'
        assert messages_at_send[-1]['content'] == 'ok', 'delivery preceded the durable assistant row'
    finally:
        release.set()
        await async_wait_until(lambda: not adapter._session_tasks, timeout=15, what='the delivered turn to finish')
        await asyncio.to_thread(authority.db.close)
