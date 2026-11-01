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
from dataclasses import dataclass, field

import pytest

ECHO = '🎙️ "queued hello"'


@dataclass
class _Rig:
    runner: object
    adapter: object
    authority: object
    source: object
    key: str
    tmp_path: object
    sent: list
    prepared: dict
    stt_calls: list = field(default_factory=list)
    stt_started: threading.Event = field(default_factory=threading.Event)
    release_stt: threading.Event = field(default_factory=threading.Event)
    release_first: asyncio.Event = field(default_factory=asyncio.Event)
    stt_results: list = field(default_factory=list)
    sid: str = ''

    def voice(self, filename='audio_7b4ba7f04459.ogg'):
        """The voice note as delivered: a redelivery (lost ACK, relay replay) carries the same
        update, message and provider timestamp, re-downloaded under a fresh staging name."""
        from datetime import datetime, timezone
        from gateway.platforms.event import MessageEvent, MessageType
        clip = self.tmp_path / filename
        clip.write_bytes(b'OggS fixture voice note')
        return MessageEvent(text='', message_type=MessageType.VOICE, source=self.source, message_id='voice',
                            platform_update_id=2, timestamp=datetime(2026, 10, 31, 19, 19, 51, tzinfo=timezone.utc),
                            media_urls=[str(clip)], media_types=['audio/ogg'])

    def rows(self):
        """Admissions of the conversation the first turn runs in (a /new rotates the route away)."""
        from hermes_state_runtime import list_session_admissions
        return [(row['request_id'], row['status'])
                for row in list_session_admissions(self.authority.db, session_id=self.sid, pending_only=False)]

    async def until(self, predicate, what, seconds=10):
        for _ in range(int(seconds * 100)):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f'timed out waiting for {what}: rows={self.rows()} sent={self.sent}')


async def _busy_rig(tmp_path, monkeypatch):
    """A Telegram topic whose first turn is running and held open until ``release_first``."""
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
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
    sent, prepared = [], {}

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)
        return SendResult(success=True, message_id=f's{len(sent)}')

    monkeypatch.setattr(adapter, 'send', send)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42', thread_id='385461')
    rig = _Rig(runner, adapter, authority, source, runner.session_store._generate_session_key(source),
               tmp_path, sent, prepared)

    def transcribe_audio(path, *_args):
        rig.stt_calls.append(path)
        rig.stt_started.set()
        assert rig.release_stt.wait(timeout=10)
        if rig.stt_results:
            return rig.stt_results.pop(0)
        return {'success': True, 'transcript': 'queued hello', 'provider': 'fixture'}

    monkeypatch.setattr('tools.transcription_tools.transcribe_audio', transcribe_audio)
    monkeypatch.setattr('tools.transcription_tools.transcribe_audio_local_fallback',
                        lambda *_args: {'success': False, 'error': 'fixture: no local STT'})
    first_running = asyncio.Event()

    async def turn(event, source, quick_key, run_generation):
        if event.message_id == 'first':
            first_running.set()
            await rig.release_first.wait()
            return 'first done'
        # The real turn's preprocessing call (run_turn_prepare): STT, echo, sender context.
        prepared[event.message_id] = await runner._prepare_profile_scoped_inbound_message_text(
            event=event, source=source, history=[], session_key=quick_key,
        )
        return 'voice done'

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    await adapter.handle_message(MessageEvent(text='long task', source=source, message_id='first',
                                              platform_update_id=1))
    await asyncio.wait_for(first_running.wait(), 10)
    assert runner._is_session_running(rig.key)
    rig.sid = runner.session_store.peek_session_id(rig.key)
    return rig


@pytest.mark.asyncio
async def test_voice_queued_behind_running_turn_echoes_before_it_finishes_and_is_transcribed_once(
        tmp_path, monkeypatch):
    rig = await _busy_rig(tmp_path, monkeypatch)
    await rig.adapter.handle_message(rig.voice())

    # STT starts while the first turn is still running, and the echo lands before it finishes.
    assert await asyncio.to_thread(rig.stt_started.wait, 10), 'queued voice was not transcribed until its turn'
    rig.release_stt.set()
    await rig.until(lambda: ECHO in rig.sent, 'the transcript echo')
    assert not rig.release_first.is_set() and rig.runner._is_session_running(rig.key)
    assert ('voice', 'queued') in rig.rows()
    assert 'voice' not in rig.prepared  # the model has not seen it: FIFO order holds

    # The running turn ends; the queued turn runs from the committed row and reuses the transcript.
    rig.release_first.set()
    await rig.until(lambda: 'voice' in rig.prepared, 'the queued voice turn')
    assert '"queued hello"' in rig.prepared['voice']
    assert len(rig.stt_calls) == 1
    assert rig.sent.count(ECHO) == 1
    assert not rig.runner.__dict__.get('_admitted_voice_prefetches')


@pytest.mark.asyncio
async def test_redelivered_voice_note_is_transcribed_and_echoed_once(tmp_path, monkeypatch):
    """A lost ACK or relay replay redelivers the same message as a fresh download. Admission
    reconciles it onto the queued row; it must not start a second STT, echo again, or make the
    claimed turn miss the first transcript and transcribe a third time."""
    rig = await _busy_rig(tmp_path, monkeypatch)
    await rig.adapter.handle_message(rig.voice())
    assert await asyncio.to_thread(rig.stt_started.wait, 10)
    rig.release_stt.set()
    await rig.until(lambda: ECHO in rig.sent, 'the transcript echo')

    await rig.adapter.handle_message(rig.voice(filename='audio_0d1e2f3a4b5c.ogg'))
    await rig.until(lambda: [r for r, _ in rig.rows()].count('voice') == 1, 'the reconciled redelivery')
    await asyncio.sleep(0.2)
    rig.release_first.set()
    await rig.until(lambda: 'voice' in rig.prepared, 'the queued voice turn')
    assert '"queued hello"' in rig.prepared['voice']
    assert len(rig.stt_calls) == 1, rig.stt_calls
    assert rig.sent.count(ECHO) == 1, rig.sent


@pytest.mark.asyncio
async def test_failed_prefetch_is_retried_by_the_turn_against_the_committed_copy(tmp_path, monkeypatch):
    """A provider failure during the prefetch is not cached into the turn: the turn transcribes the
    committed copy again, and no note points at the adapter's download, which is not retained."""
    rig = await _busy_rig(tmp_path, monkeypatch)
    rig.stt_results.append({'success': False, 'error': 'fixture: provider down'})
    voice = rig.voice()
    download = voice.media_urls[0]
    await rig.adapter.handle_message(voice)
    assert await asyncio.to_thread(rig.stt_started.wait, 10)
    rig.release_stt.set()
    await rig.until(lambda: len(rig.stt_calls) == 1 and not rig.stt_results, 'the failed prefetch')
    await asyncio.sleep(0.2)
    assert ECHO not in rig.sent

    rig.release_first.set()
    await rig.until(lambda: 'voice' in rig.prepared, 'the queued voice turn')
    assert '"queued hello"' in rig.prepared['voice']
    assert download not in rig.prepared['voice']
    assert len(rig.stt_calls) == 2
    assert rig.stt_calls[1] != download  # the retry reads the committed copy
    assert rig.sent.count(ECHO) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['/stop', '/new'])
async def test_fenced_queued_voice_never_echoes_late(tmp_path, monkeypatch, command):
    """/stop or /new cancels the queued voice row; its in-flight STT must not echo afterwards."""
    from gateway.platforms.event import MessageEvent, MessageType
    rig = await _busy_rig(tmp_path, monkeypatch)
    await rig.adapter.handle_message(rig.voice())
    assert await asyncio.to_thread(rig.stt_started.wait, 10)

    await rig.adapter.handle_message(MessageEvent(text=command, message_type=MessageType.COMMAND,
                                                  source=rig.source, message_id='cmd', platform_update_id=9))
    await rig.until(lambda: ('voice', 'terminal') in rig.rows(), 'the cancelled voice row')
    await rig.until(lambda: not rig.runner.__dict__.get('_admitted_voice_prefetches'), 'the prefetch release')
    rig.release_stt.set()
    rig.release_first.set()
    await asyncio.sleep(0.5)
    assert ECHO not in rig.sent, rig.sent
    assert 'voice' not in rig.prepared
