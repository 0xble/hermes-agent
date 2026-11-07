"""A slow goal judge must run after the durable answer reaches its transport."""

import asyncio

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
