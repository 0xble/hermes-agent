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
@pytest.mark.parametrize('identified', [False, True])
async def test_merged_inputs_all_own_first_visible_delivery(monkeypatch, tmp_path, caplog, restore, identified):
    from gateway.session_envelope import _adapter_event, _snapshot_native, restore_native
    caplog.set_level('INFO', logger='gateway.reply_latency')
    adapter = Adapter()
    first, second = event(adapter, 'one' if identified else None), event(adapter, 'two' if identified else None)
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
        latency._live.clear()  # emulate native restoration after a process restart
        caplog.clear()
        incoming = restore_native(payload)
    latency.bind_agent_start(incoming)
    await adapter.send_final_ledgered(incoming, 'route', 'answer to both', {}, reply_to=None)
    await adapter.send_final_ledgered(incoming, 'route', 'another answer', {}, reply_to=None)
    replies = [r for r in records(caplog) if r['event'] == 'first_reply']
    assert sorted(r['message_id'] or '' for r in replies) == (['one', 'two'] if identified else ['', ''])
    if restore:
        assert sorted(r['timestamp'] for r in records(caplog) if r['event'] == 'received') == [100, 110]
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


@pytest.mark.parametrize('thread_id', [None, '', 'topic'])
def test_reaction_is_visible_reply_only_after_success(monkeypatch, caplog, thread_id):
    import tools.react_tool as react
    import tools.send_message_senders as senders
    import tools.send_message_tool as send_tool
    import model_tools
    from gateway.session_context import clear_session_vars, set_session_vars

    caplog.set_level('INFO', logger='gateway.reply_latency')
    incoming = event(Adapter(), 'reaction', thread_id)
    latency.bind_agent_start(incoming)
    reaction = AsyncMock(return_value=False)
    monkeypatch.setattr(senders, '_live_adapter', lambda platform: (None, SimpleNamespace(add_reaction=reaction)))
    async def dispatch(runner, call, error):
        return await call()
    monkeypatch.setattr(send_tool, '_dispatch_on_gateway_loop', dispatch)
    monkeypatch.setattr(model_tools, '_run_async', asyncio.run)
    # Match GatewayRunner._set_session_env: no thread is bound as '', not None.
    tokens = set_session_vars(platform='telegram', chat_id='chat', message_id='reaction',
                              thread_id=thread_id or '', scope_id='', user_id='', parent_chat_id='')
    try:
        assert 'error' in json.loads(react._react_gateway(Platform.TELEGRAM, '👍'))
        assert not incoming._reply_latency_receipt.replied
        reaction.return_value = True
        assert json.loads(react._react_gateway(Platform.TELEGRAM, '👍'))['success']
        reaction.assert_awaited_with('chat', 'reaction', '👍', scope_id='', thread_id=thread_id or '',
                                     user_id='', parent_chat_id='', team_id='')
        latency.record_no_reply(incoming._reply_latency_receipt)
        assert [r['event'] for r in records(caplog)] == ['received', 'agent_start', 'first_reply']
    finally:
        clear_session_vars(tokens)


@pytest.mark.parametrize('thread_id', [None, '', 'topic', 0])
def test_send_message_only_current_route_and_confirmed_delivery(monkeypatch, caplog, thread_id):
    import tools.send_message_tool as tool
    import model_tools
    caplog.set_level('INFO', logger='gateway.reply_latency')
    incoming = event(Adapter(), 'tool', thread_id)
    latency.bind_agent_start(incoming)
    # Target parsers return None for an unthreaded destination, even if intake used ''.
    destination_thread_id = None if thread_id in (None, '') else thread_id
    monkeypatch.setattr(tool, '_resolve_tool_target', lambda target: (
        'telegram', target.split(':')[0], 'other-topic' if ':' in target else destination_thread_id, None))
    monkeypatch.setattr('gateway.config.load_gateway_config', lambda: object())
    monkeypatch.setattr(tool, '_resolve_platform_config', lambda *args: (Platform.TELEGRAM, object(), None, None))
    monkeypatch.setattr(tool, '_authorize_relay_target', lambda *args, **kwargs: None)
    monkeypatch.setattr(tool, '_mirror_sent_message', lambda *args: False)
    monkeypatch.setattr(tool, '_maybe_skip_cron_duplicate_send', lambda *args: None)
    success, skipped = False, False
    async def send(*args, **kwargs):
        return {'success': success, 'skipped': skipped}
    monkeypatch.setattr(tool, '_send_to_platform', send)
    monkeypatch.setattr(model_tools, '_run_async', asyncio.run)
    tool._handle_send({'target': 'chat', 'message': 'failed'})
    success, skipped = True, True
    tool._handle_send({'target': 'chat', 'message': 'skipped'})
    skipped = False
    tool._handle_send({'target': 'other', 'message': 'not an answer here'})
    tool._handle_send({'target': 'chat:other-topic', 'message': 'not an answer in this thread'})
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


@pytest.mark.asyncio
async def test_tool_first_chunk_is_recorded_before_later_failure(caplog):
    from tools.send_message_tool import _send_chunks
    caplog.set_level('INFO', logger='gateway.reply_latency')
    incoming = event(Adapter(), 'chunked-tool')
    latency.bind_agent_start(incoming)
    sent = 0
    async def send(chunk, is_last):
        nonlocal sent
        sent += 1
        if is_last:
            assert incoming._reply_latency_receipt.replied
            return {'error': 'second chunk failed'}
        return {'success': True}
    result = await _send_chunks(['first', 'second'], send,
                               lambda: latency.record_tool_reply('telegram', 'chat'))
    assert result['error'] == 'second chunk failed'
    assert len([r for r in records(caplog) if r['event'] == 'first_reply']) == 1


def test_terminal_receipt_is_atomic_and_silence_never_overwrites_delivery(caplog):
    from concurrent.futures import ThreadPoolExecutor
    caplog.set_level('INFO', logger='gateway.reply_latency')
    incoming = event(Adapter(), 'atomic')
    receipt = latency.record_received(incoming)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: latency.record_first_reply(receipt, 'delivered'), range(16)))
    latency.record_no_reply(receipt)
    assert [r['event'] for r in records(caplog)] == ['received', 'first_reply']


@pytest.mark.asyncio
async def test_voice_delivery_counts_only_confirmed_success(caplog):
    from gateway.run_voice import GatewayVoiceMixin
    caplog.set_level('INFO', logger='gateway.reply_latency')
    incoming = event(Adapter(), 'voice')
    latency.bind_agent_start(incoming)
    adapter = SimpleNamespace(send_voice=AsyncMock(side_effect=[None, SendResult(success=False), SendResult(success=True)]))
    runner = SimpleNamespace(_delivery_adapter_for=lambda source: adapter, _get_guild_id=lambda e: None,
                             _reply_anchor_for_event=lambda e: None, _thread_metadata_for_source=lambda *a: {})
    await GatewayVoiceMixin._deliver_voice_reply(runner, incoming, ['one', 'two', 'three'])
    assert [r['event'] for r in records(caplog)] == ['received', 'agent_start', 'first_reply']


@pytest.mark.asyncio
async def test_streaming_tts_uses_captured_receipt_at_first_audible_chunk(monkeypatch, caplog):
    from gateway.streaming_tts_consumer import StreamingTTSConsumer
    caplog.set_level('INFO', logger='gateway.reply_latency')
    incoming = event(Adapter(), 'stream-voice')
    latency.bind_agent_start(incoming)
    monkeypatch.setattr('tools.tts_streaming.resolve_streaming_provider', lambda c: None)
    adapter = SimpleNamespace(write_streaming_tts=AsyncMock())
    consumer = StreamingTTSConsumer(adapter, 'chat', {}, asyncio.get_running_loop())
    consumer._streamer = SimpleNamespace(stream=lambda text: iter([b'audio', b'more']))
    consumer._handle = SimpleNamespace(aborted=False, audible=False)
    consumer._strip_markdown = lambda text: text
    latency.restore_receipt(None)
    await consumer._synthesise_and_write('spoken answer')
    assert [r['event'] for r in records(caplog)] == ['received', 'agent_start', 'first_reply']
