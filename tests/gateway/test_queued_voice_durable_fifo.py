"""A voice note queued behind a running turn is transcribed and echoed now, through the durable FIFO.

Under session authority a busy Telegram follow-up is committed to the durable FIFO and later runs
as a NEW event restored from that row. The 2026-09-23 eager STT only hooked the in-process pending
slot, so on the live path the transcript waited for the running turn to finish (2026-10-31: a voice
note received at 12:19:53 PT was transcribed at 12:25:39 and echoed at 12:25:50). Real
TelegramAdapter dispatch, real GatewayRunner busy handling and admission, real authority over a
real SessionDB, real restore and inbound text preparation; only the STT provider and the turn's
agent call are stubbed.
"""
import asyncio
import threading

import pytest


@pytest.mark.asyncio
async def test_voice_queued_behind_running_turn_echoes_before_it_finishes_and_is_transcribed_once(
        tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from hermes_state_runtime import list_session_admissions
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

    sent = []

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)
        return SendResult(success=True, message_id=f's{len(sent)}')

    monkeypatch.setattr(adapter, 'send', send)

    stt_calls, stt_started, release_stt = [], threading.Event(), threading.Event()

    def transcribe_audio(path, *_args):
        stt_calls.append(path)
        stt_started.set()
        assert release_stt.wait(timeout=10)
        return {'success': True, 'transcript': 'queued hello', 'provider': 'fixture'}

    monkeypatch.setattr('tools.transcription_tools.transcribe_audio', transcribe_audio)

    first_running, release_first = asyncio.Event(), asyncio.Event()
    prepared = {}

    async def turn(event, source, quick_key, run_generation):
        if event.message_id == 'first':
            first_running.set()
            await release_first.wait()
            return 'first done'
        # The real turn's preprocessing call (run_turn_prepare): STT, echo, sender context.
        prepared[event.message_id] = await runner._prepare_profile_scoped_inbound_message_text(
            event=event, source=source, history=[], session_key=quick_key,
        )
        return 'voice done'

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42', thread_id='385461')
    key = runner.session_store._generate_session_key(source)

    first = MessageEvent(text='long task', source=source, message_id='first', platform_update_id=1)
    await adapter.handle_message(first)
    await asyncio.wait_for(first_running.wait(), 10)
    assert runner._is_session_running(key)

    clip = tmp_path / 'audio_7b4ba7f04459.ogg'
    clip.write_bytes(b'OggS fixture voice note')
    voice = MessageEvent(text='', message_type=MessageType.VOICE, source=source, message_id='voice',
                         platform_update_id=2, media_urls=[str(clip)], media_types=['audio/ogg'])
    await adapter.handle_message(voice)

    # STT starts while the first turn is still running, and the echo lands before it finishes.
    assert await asyncio.to_thread(stt_started.wait, 10), 'queued voice was not transcribed until its turn'
    release_stt.set()
    for _ in range(500):
        if '🎙️ "queued hello"' in sent:
            break
        await asyncio.sleep(0.01)
    assert '🎙️ "queued hello"' in sent
    assert not release_first.is_set() and runner._is_session_running(key)
    sid = runner.session_store.peek_session_id(key)
    assert 'queued' in [row['status'] for row in list_session_admissions(authority.db, session_id=sid)]
    assert 'voice' not in prepared  # the model has not seen it: FIFO order holds

    # The running turn ends; the queued turn runs from the committed row and reuses the transcript.
    release_first.set()
    for _ in range(1000):
        if 'voice' in prepared:
            break
        await asyncio.sleep(0.01)
    assert '"queued hello"' in prepared['voice']
    assert len(stt_calls) == 1
    assert sent.count('🎙️ "queued hello"') == 1
    assert not runner.__dict__.get('_admitted_voice_prefetches')
