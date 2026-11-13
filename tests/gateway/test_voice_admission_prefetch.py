"""Every durable voice entry point starts STT before turn preparation, not after a busy check."""
import asyncio
import threading
from pathlib import Path

import pytest
import pytest_asyncio

from tests.waiting import async_wait_until


@pytest_asyncio.fixture
async def voice_lane(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from tests.fakes.runtime_owner import initialize_session_authority

    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '42')
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_ACK_ENABLED', 'false')
    runner = GatewayRunner(GatewayConfig())
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    adapter.gateway_runner = runner
    runner.adapters = {Platform.TELEGRAM: adapter}
    monkeypatch.setattr(runner, '_read_user_config', dict)
    monkeypatch.setattr(runner, '_effective_busy_input_mode', lambda _source: 'queue')
    monkeypatch.setattr(adapter, '_start_typing_refresh', lambda *a, **kw: None)
    sent, calls, timeline = [], [], []
    release_stt = threading.Event()
    prepare_turns = asyncio.Event()
    entered = asyncio.Event()
    prepared = {}

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)
        return SendResult(success=True, message_id=f's{len(sent)}')

    def transcribe(path, *_args):
        name = Path(path).name
        calls.append(name)
        timeline.append(('stt', name))
        assert release_stt.wait(10), 'test did not release STT'
        return {'success': True, 'transcript': name, 'provider': 'fixture'}

    async def turn(event, source, quick_key, run_generation):
        entered.set()
        await prepare_turns.wait()
        timeline.append(('prepare', event.message_id))
        prepared[event.message_id] = await runner._prepare_profile_scoped_inbound_message_text(
            event=event, source=source, history=[], session_key=quick_key)
        return 'done'

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr('tools.transcription_tools.transcribe_audio', transcribe)
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    admitted = authority._admitted

    def record_commit(ref, event, row):
        receipt = admitted(ref, event, row)
        timeline.append(('commit', event.message_id))
        return receipt

    monkeypatch.setattr(authority, '_admitted', record_commit)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42', thread_id='voice')
    lane = dict(runner=runner, adapter=adapter, authority=authority, source=source,
                sent=sent, calls=calls, timeline=timeline, release_stt=release_stt,
                prepare_turns=prepare_turns, entered=entered, prepared=prepared)
    try:
        yield lane
    finally:
        release_stt.set()
        prepare_turns.set()
        await async_wait_until(lambda: not any(live.task and not live.task.done()
                                               for live in authority.sessions.values()), timeout=10)
        for task in list(adapter._background_tasks):
            task.cancel()
        await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)


def voice_event(tmp_path, lane, index):
    from gateway.platforms.event import MessageEvent, MessageType
    clip = tmp_path / f'clip-{index}.ogg'
    clip.write_bytes(b'OggS fixture ' + bytes([index]))
    return MessageEvent(text='', source=lane['source'], message_type=MessageType.VOICE,
                        message_id=f'voice-{index}', platform_update_id=index,
                        media_urls=[str(clip)], media_types=['audio/ogg'])


@pytest.mark.asyncio
@pytest.mark.parametrize('admission', ['idle', 'claim_race', 'started', 'queue', 'relay'])
async def test_audio_starts_at_admission_and_preparation_adopts_each_clip(
        voice_lane, tmp_path, monkeypatch, admission):
    from gateway.platforms.event import MessageEvent
    from hermes_state_runtime import list_session_admissions

    lane = voice_lane
    runner, adapter, authority = (lane[name] for name in ('runner', 'adapter', 'authority'))
    key = runner.session_store._generate_session_key(lane['source'])
    assert not runner._is_session_running(key)
    if admission == 'claim_race':
        # Hold an earlier durable turn before it claims the RAM slot. The snapshot says idle,
        # even though a turn now owns the lane and the next two inputs must remain FIFO.
        first = MessageEvent(text='long task', source=lane['source'], message_id='first')
        await adapter.handle_message(first)
        await asyncio.wait_for(lane['entered'].wait(), 10)
        monkeypatch.setattr(runner, '_is_session_running', lambda _key: False)
    if admission == 'started':
        original = authority.admit_native

        async def already_started(event):
            receipt = await original(event)
            await async_wait_until(lambda: any(row['status'] == 'started'
                for row in list_session_admissions(authority.db, session_id=receipt.ref.session_id)), timeout=10)
            return receipt

        monkeypatch.setattr(authority, 'admit_native', already_started)
    for index in (1, 2):
        event = voice_event(tmp_path, lane, index)
        lane['timeline'].append(('arrival', event.message_id))
        if admission == 'queue':
            command = MessageEvent(text='/queue', source=lane['source'], message_id=f'queue-{index}')
            assert await runner._queue_command_turn(key, command, event, adapter)
        elif admission == 'relay':
            from gateway.relay.inbound_admission import admit_relay_delivery
            task = await admit_relay_delivery(adapter, runner, event)
            if task is not None:
                await task
        else:
            await adapter.handle_message(event)
        # Adapter ingress is backgrounded: fix the first clip's durable FIFO position before
        # submitting the next one, rather than asserting task scheduling order before admission.
        await async_wait_until(lambda: ('commit', event.message_id) in lane['timeline'], timeout=10)
    try:
        await async_wait_until(lambda: len(lane['calls']) == 2, timeout=3)
        assert not lane['prepare_turns'].is_set(), 'STT waited for the turn to prepare or end'
        assert sorted(lane['calls']) == ['clip-1.ogg', 'clip-2.ogg']
        for index in (1, 2):
            assert lane['timeline'].index(('arrival', f'voice-{index}')) < lane['timeline'].index(
                ('commit', f'voice-{index}')) < lane['timeline'].index(('stt', f'clip-{index}.ogg'))
        assert not any(kind == 'prepare' for kind, _ in lane['timeline'])
        lane['release_stt'].set()
        await async_wait_until(lambda: len([text for text in lane['sent'] if text.startswith('🎙️')]) == 2,
                               timeout=10)
    finally:
        lane['release_stt'].set()
        lane['prepare_turns'].set()
    await async_wait_until(lambda: all(f'voice-{i}' in lane['prepared'] for i in (1, 2)), timeout=10)
    assert len(lane['calls']) == 2
    assert lane['sent'].count('🎙️ "clip-1.ogg"') == 1
    assert lane['sent'].count('🎙️ "clip-2.ogg"') == 1
    assert 'clip-1.ogg' in lane['prepared']['voice-1']
    assert 'clip-2.ogg' in lane['prepared']['voice-2']
    assert [name for kind, name in lane['timeline'] if kind == 'prepare'] == (
        ['first', 'voice-1', 'voice-2'] if admission == 'claim_race' else ['voice-1', 'voice-2'])
    # Text still takes the same turn path and never invokes the STT provider.
    text = MessageEvent(text='plain hello', source=lane['source'], message_id='text')
    await adapter.handle_message(text)
    await async_wait_until(lambda: 'text' in lane['prepared'], timeout=10)
    assert 'plain hello' in lane['prepared']['text']
    assert len(lane['calls']) == 2


@pytest.mark.asyncio
async def test_recovered_durable_voice_prefetches_before_replayed_turn_prepares(
        voice_lane, tmp_path, monkeypatch):
    from tests.fakes.runtime_owner import initialize_session_authority

    lane = voice_lane
    runner, authority = lane['runner'], lane['authority']
    # Persist without executing, then discard all process-local STT state as a restart does.
    with monkeypatch.context() as before_restart:
        before_restart.setattr(authority, '_schedule', lambda _ref: None)
        before_restart.setattr(runner, '_prefetch_admitted_voice', lambda _event: None)
        receipt = await authority.admit_native(voice_event(tmp_path, lane, 1))
    assert not lane['calls']
    recovered = await initialize_session_authority(
        runner, profile_id='default', instance_id='restarted', db=authority.db)
    result = await recovered.recover_native_sessions(
        [(receipt.ref.session_id, lane['source'], lane['adapter'])])
    assert result == {receipt.ref.session_id: 'ready'}
    try:
        await async_wait_until(lambda: len(lane['calls']) == 1, timeout=3)
        assert not lane['prepare_turns'].is_set()
        # Repeating restore while it is queued/claimed must adopt the same clip memo.
        await recovered.recover_native_sessions(
            [(receipt.ref.session_id, lane['source'], lane['adapter'])])
    finally:
        lane['release_stt'].set()
        lane['prepare_turns'].set()
    await async_wait_until(lambda: 'voice-1' in lane['prepared'], timeout=10)
    await async_wait_until(lambda: not any(live.task and not live.task.done()
        for live in recovered.sessions.values()), timeout=10)
    assert lane['calls'] == ['clip-1.ogg']
    assert lane['sent'].count('🎙️ "clip-1.ogg"') == 1


@pytest.mark.asyncio
async def test_plain_text_admission_does_not_start_stt(voice_lane):
    from gateway.platforms.event import MessageEvent
    lane = voice_lane
    await lane['adapter'].handle_message(
        MessageEvent(text='plain hello', source=lane['source'], message_id='text'))
    await asyncio.wait_for(lane['entered'].wait(), 10)
    assert not lane['calls']
    lane['prepare_turns'].set()
    await async_wait_until(lambda: 'text' in lane['prepared'], timeout=10)
    assert 'plain hello' in lane['prepared']['text']
    assert not lane['calls']
