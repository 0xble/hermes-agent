"""First visible delivery belongs to the executed input, not an adapter copy."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import reply_latency as latency
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, SendResult, merge_pending_message_event
from tests.gateway.test_reply_latency_logs import Adapter, event, records
from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize('streamed', [False, True])
async def test_native_waiter_and_busy_copy_share_executed_receipt(monkeypatch, caplog, streamed):
    from gateway.run import GatewayRunner
    from gateway.session_results import execution_result
    from tests.fakes.runtime_owner import initialize_session_authority
    from tests.gateway.test_shared_busy_receive_loop import Adapter as BusyAdapter
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

    caplog.set_level('INFO', logger='gateway.reply_latency')
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', 'fixture-user')
    runner = GatewayRunner(GatewayConfig())
    adapter = BusyAdapter()
    adapter.gateway_runner = runner
    runner.adapters = {Platform.TELEGRAM: adapter}
    await initialize_session_authority(runner, profile_id='default', instance_id='latency-owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='busy-chat', chat_type='dm', user_id='fixture-user')
    gate = asyncio.Event()

    async def turn(incoming, source, quick_key, run_generation):
        adapter.turn_started[incoming.text].set()
        if incoming.text == 'one':
            await gate.wait()
        if streamed:
            consumer = GatewayStreamConsumer(adapter, source.chat_id, StreamConsumerConfig(cursor=''))
            await consumer._send_or_edit('stream ' + incoming.text, finalize=True)
        execution_result.get()['result'] = {'final_response': 'reply ' + incoming.text, 'messages': []}
        return 'reply ' + incoming.text

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    await adapter.handle_message(MessageEvent(text='one', source=source, message_id='m1'))
    await asyncio.wait_for(adapter.turn_started['one'].wait(), 5)
    await asyncio.wait_for(adapter.handle_message(MessageEvent(text='two', source=source, message_id='m2')), 5)
    gate.set()
    await async_wait_until(lambda: 'reply two' in adapter.sent and not adapter._background_tasks, 15)
    replies = [r for r in records(caplog) if r['event'] == 'first_reply']
    assert [r['message_id'] for r in replies] == ['m1', 'm2']


@pytest.mark.asyncio
@pytest.mark.parametrize('restore', [False, True])
async def test_merged_inputs_all_own_first_visible_delivery(monkeypatch, tmp_path, caplog, restore):
    from gateway.session_envelope import _adapter_event, _snapshot_native, restore_native
    caplog.set_level('INFO', logger='gateway.reply_latency')
    adapter = Adapter()
    first, second = event(adapter, 'one'), event(adapter, 'two')
    first._attended_activity_received_at = 100
    second._attended_activity_received_at = 110
    latency.record_received(first)
    latency.record_received(second)
    pending = {'route': first}
    merge_pending_message_event(pending, 'route', second, merge_text=True)
    incoming = pending['route']
    if restore:
        runner = SimpleNamespace(session_store=SimpleNamespace(_generate_session_key=lambda source: 'route'))
        payload = _snapshot_native(runner, _adapter_event(incoming), None,
                                  encoded_source={**incoming.source.to_dict(), 'is_bot': False})
        incoming = restore_native(payload)
    latency.bind_agent_start(incoming)
    await adapter.send_final_ledgered(incoming, 'route', 'answer to both', {}, reply_to=None)
    await adapter.send_final_ledgered(incoming, 'route', 'another answer', {}, reply_to=None)
    replies = [r for r in records(caplog) if r['event'] == 'first_reply']
    assert sorted(r['message_id'] for r in replies) == ['one', 'two']
    assert {r['timestamp'] for r in replies}.__len__() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('token', ['NO_REPLY', '[SILENT]'])
async def test_intentional_silence_emits_terminal_not_latency(monkeypatch, tmp_path, caplog, token):
    from tests.gateway.test_gateway_silence_tokens import _runner, _event
    caplog.set_level('INFO', logger='gateway.reply_latency')
    runner = _runner(monkeypatch, tmp_path)
    incoming = _event(reply_expected=False)
    latency.bind_agent_start(incoming)
    runner._run_agent = AsyncMock(return_value={'final_response': token, 'messages': [], 'tools': [],
        'history_offset': 0, 'last_prompt_tokens': 0, 'api_calls': 1, 'failed': False})
    assert not await runner._handle_message_with_agent(incoming, incoming.source, 'route', 1)
    terminal = [r['event'] for r in records(caplog) if r['event'] in ('first_reply', 'no_reply')]
    assert terminal == ['no_reply']


def test_reaction_is_visible_reply_only_after_success(monkeypatch, caplog):
    import tools.react_tool as react
    import tools.send_message_senders as senders
    import tools.send_message_tool as send_tool
    import model_tools
    caplog.set_level('INFO', logger='gateway.reply_latency')
    incoming = event(Adapter(), 'reaction')
    latency.bind_agent_start(incoming)
    env = {'HERMES_SESSION_CHAT_ID': 'chat', 'HERMES_SESSION_MESSAGE_ID': 'reaction'}
    monkeypatch.setattr(react, 'get_session_env', lambda key: env.get(key))
    monkeypatch.setattr(senders, '_live_adapter', lambda platform: (None, SimpleNamespace(add_reaction=object())))
    async def dispatch(*args):
        return True
    monkeypatch.setattr(send_tool, '_dispatch_on_gateway_loop', dispatch)
    monkeypatch.setattr(model_tools, '_run_async', asyncio.run)
    result = json.loads(react._react_gateway(Platform.TELEGRAM, '👍'))
    assert result['success']
    assert [r['event'] for r in records(caplog)] == ['received', 'agent_start', 'first_reply']


def test_send_message_only_current_route_and_confirmed_delivery(monkeypatch, caplog):
    import tools.send_message_tool as tool
    import model_tools
    caplog.set_level('INFO', logger='gateway.reply_latency')
    incoming = event(Adapter(), 'tool', 'topic')
    latency.bind_agent_start(incoming)
    monkeypatch.setattr(tool, '_resolve_tool_target', lambda target: ('telegram', target, 'topic', None))
    monkeypatch.setattr('gateway.config.load_gateway_config', lambda: object())
    monkeypatch.setattr(tool, '_resolve_platform_config', lambda *args: (Platform.TELEGRAM, object(), None, None))
    monkeypatch.setattr(tool, '_authorize_relay_target', lambda *args, **kwargs: None)
    monkeypatch.setattr(tool, '_mirror_sent_message', lambda *args: False)
    monkeypatch.setattr(tool, '_maybe_skip_cron_duplicate_send', lambda *args: None)
    success = False
    async def send(*args, **kwargs):
        return {'success': success}
    monkeypatch.setattr(tool, '_send_to_platform', send)
    monkeypatch.setattr(model_tools, '_run_async', asyncio.run)
    tool._handle_send({'target': 'chat', 'message': 'failed'})
    success = True
    tool._handle_send({'target': 'other', 'message': 'not an answer here'})
    assert not [r for r in records(caplog) if r['event'] == 'first_reply']
    tool._handle_send({'target': 'chat', 'message': 'answer'})
    tool._handle_send({'target': 'chat', 'message': 'second answer'})
    assert len([r for r in records(caplog) if r['event'] == 'first_reply']) == 1


@pytest.mark.asyncio
async def test_attachment_only_success_counts_as_visible_reply(caplog):
    caplog.set_level('INFO', logger='gateway.reply_latency')
    adapter = Adapter()
    incoming = event(adapter, 'media')
    latency.bind_agent_start(incoming)
    extracted = SimpleNamespace(images=[('https://example.invalid/image.png', '')], media_files=[],
        local_files=[], force_document_attachments=False, pre_extract='image')
    async def images(event, images, metadata, delay, record):
        record(SendResult(success=False))
        assert not incoming._reply_latency_receipt.replied
        record(SendResult(success=True))
    adapter._send_image_batch = images
    await adapter._deliver_attachments(incoming, extracted, {}, anything_sent=False, record_delivery=lambda r: None)
    assert incoming._reply_latency_receipt.replied
