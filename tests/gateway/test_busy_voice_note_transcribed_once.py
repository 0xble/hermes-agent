"""A voice note that interrupts a busy turn is transcribed and echoed ONCE, though the session
authority's FIFO runs it from a freshly restored event (#152457).

The busy preempt interrupts at once with the media placeholder (#143961). The fork starts STT
at durable admission; the FIFO turn reuses that result and only retries refused echoes.
"""
import asyncio
import os
from contextlib import asynccontextmanager

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session_authority import SessionAuthority
from tests.fakes.runtime_owner import initialize_session_authority
from gateway.session_envelope import restore_native
from hermes_state_runtime import list_session_admissions
from tests.waiting import async_wait_until

CHAT = '15551234567@s.whatsapp.net'


class _RunningAgent:
    def __init__(self):
        self.interrupts = []

    def interrupt(self, *args):
        self.interrupts.append(args)

    def get_activity_summary(self):
        return {}


async def _busy_rig(tmp_path, monkeypatch):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
    monkeypatch.setenv('WHATSAPP_ALLOWED_USERS', '15551234567')
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={'session_path': str(tmp_path / 'wa')}))
    adapter.gateway_runner, runner.adapters = runner, {Platform.WHATSAPP: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='busy-voice')
    runner._wire_adapter_handlers(adapter)
    sent = []

    async def send(chat_id, content, **_):
        sent.append((chat_id, content))
        return SendResult(success=True, message_id='s')

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_effective_busy_input_mode', lambda source: 'interrupt')
    monkeypatch.setattr(runner, '_should_echo_stt_transcripts', lambda: True)
    monkeypatch.setattr(runner, '_handle_message_with_agent', lambda *a, **k: None)
    source = adapter.build_source(chat_id=CHAT, user_id=CHAT, chat_type='dm')
    turn = runner._session_state(runner._session_key_for_source(source)).turn
    turn.agent = _RunningAgent()
    turn.event = MessageEvent(text='long task', source=source, message_id='t0')
    return runner, authority, source, turn.agent, sent


def _rows(authority):
    return [row for sid in authority.sessions
            for row in list_session_admissions(authority.db, session_id=sid, pending_only=False)]


@pytest.mark.asyncio
async def test_busy_voice_note_is_transcribed_and_echoed_once(tmp_path, monkeypatch):
    calls = []

    def transcribe_audio(path, model=None, source=None):
        calls.append(bool(agent.interrupts))
        return {'success': True, 'transcript': 'stop and check the logs', 'provider': 'fake'}

    runner, authority, source, agent, sent = await _busy_rig(tmp_path, monkeypatch)
    monkeypatch.setattr('tools.transcription_tools.transcribe_audio', transcribe_audio)
    clip = tmp_path / 'note.ogg'
    clip.write_bytes(b'OggS voice note bytes')
    try:
        async with _admit_busy_voice(runner, authority, source, clip) as restored:
            assert [i[0].startswith('[User sent') for i in agent.interrupts] == [True]
            await async_wait_until(lambda: _echoes(sent), timeout=10, what='admission-time transcript echo')
            assert calls == [True], 'interrupt must be signalled before STT starts'
            text = await runner._prepare_inbound_message_text(event=restored, source=restored.source, history=[])

        assert text is not None and '"stop and check the logs"' in text
        assert calls == [True]
        assert _echoes(sent) == [(CHAT, '🎙️ "stop and check the logs"')]
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_same_voice_note_sent_again_while_idle_is_echoed_again(tmp_path, monkeypatch):
    """The hand-off is consumed by the one turn it was for: the clip resent later echoes anew."""
    monkeypatch.setattr('tools.transcription_tools.transcribe_audio',
                        lambda path, model=None, source=None: {'success': True, 'transcript': 'hi', 'provider': 'f'})
    runner, authority, source, _agent, sent = await _busy_rig(tmp_path, monkeypatch)
    clip = tmp_path / 'note.ogg'
    clip.write_bytes(b'OggS same clip')
    try:
        async with _admit_busy_voice(runner, authority, source, clip) as restored:
            await runner._prepare_inbound_message_text(event=restored, source=restored.source, history=[])
        again = MessageEvent(text='', message_type=MessageType.VOICE, source=source, message_id='v2',
                             media_urls=[str(clip)], media_types=['audio/ogg'])
        await runner._prepare_inbound_message_text(event=again, source=source, history=[])

        assert _echoes(sent) == [(CHAT, '🎙️ "hi"')] * 2
    finally:
        authority.db.close()


def _echoes(sent):
    return [(chat, content) for chat, content in sent if content.startswith('🎙️')]


@asynccontextmanager
async def _admit_busy_voice(runner, authority, source, *clips):
    task = asyncio.create_task(runner._handle_message(MessageEvent(
        text='', message_type=MessageType.VOICE, source=source, message_id='v1',
        media_urls=[str(clip) for clip in clips], media_types=['audio/ogg'] * len(clips))))
    try:
        await async_wait_until(lambda: _rows(authority), timeout=10, what='the voice note admitted')
        yield restore_native(_rows(authority)[0]['payload'], runner)
    finally:
        # The real ingress waits for the drain. Cancel only after our manual drain processes the
        # restored event; cancelling first discards the shared prefetch in the ingress finalizer.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_clip_forwarded_to_another_chat_still_echoes_there(tmp_path, monkeypatch):
    """The same clip arriving idle in another chat before the busy message's FIFO turn runs is
    echoed in that chat, and the busy message's own turn echoes it once in its chat."""
    monkeypatch.setattr('tools.transcription_tools.transcribe_audio',
                        lambda path, model=None, source=None: {'success': True, 'transcript': 'hi', 'provider': 'f'})
    runner, authority, source, _agent, sent = await _busy_rig(tmp_path, monkeypatch)
    clip = tmp_path / 'note.ogg'
    clip.write_bytes(b'OggS forwarded clip')
    other = runner.adapters[Platform.WHATSAPP].build_source(
        chat_id='15557654321@s.whatsapp.net', user_id=CHAT, chat_type='dm')
    try:
        async with _admit_busy_voice(runner, authority, source, clip) as restored:
            await async_wait_until(lambda: _echoes(sent), timeout=10, what='chat A transcript echo')
            assert _echoes(sent) == [(CHAT, '🎙️ "hi"')]
            forwarded = MessageEvent(text='', message_type=MessageType.VOICE, source=other, message_id='f1',
                                     media_urls=[str(clip)], media_types=['audio/ogg'])
            await runner._prepare_inbound_message_text(event=forwarded, source=other, history=[])
            assert _echoes(sent) == [(CHAT, '🎙️ "hi"'), (other.chat_id, '🎙️ "hi"')]
            await runner._prepare_inbound_message_text(event=restored, source=restored.source, history=[])
            assert _echoes(sent) == [(CHAT, '🎙️ "hi"'), (other.chat_id, '🎙️ "hi"')]
    finally:
        authority.db.close()



@pytest.mark.asyncio
async def test_a_forwarded_copy_never_takes_the_busy_messages_transcript(tmp_path, monkeypatch):
    """A forwarded copy must get its own transcript, not consume chat A's admitted prefetch."""
    calls = []

    def transcribe(path, model=None, source=None):
        calls.append(path)
        return {'success': True, 'transcript': 'paid words' if len(calls) == 1 else 'different words', 'provider': 'f'}

    monkeypatch.setattr('tools.transcription_tools.transcribe_audio', transcribe)
    runner, authority, source, agent, sent = await _busy_rig(tmp_path, monkeypatch)
    clip = tmp_path / 'note.ogg'
    clip.write_bytes(b'OggS forwarded busy clip')
    other = runner.adapters[Platform.WHATSAPP].build_source(
        chat_id='15557654321@s.whatsapp.net', user_id=CHAT, chat_type='dm')
    try:
        async with _admit_busy_voice(runner, authority, source, clip) as restored:
            await async_wait_until(lambda: _echoes(sent), timeout=10, what='chat A transcript echo')
            # Reuse even the message ID and path so the chat identity is required for isolation.
            forwarded = MessageEvent(text='', message_type=MessageType.VOICE, source=other, message_id='v1',
                                     media_urls=[str(clip)], media_types=['audio/ogg'])
            forwarded_text = await runner._prepare_inbound_message_text(event=forwarded, source=other, history=[])
            text = await runner._prepare_inbound_message_text(event=restored, source=restored.source, history=[])
    finally:
        authority.db.close()
    assert [i[0].startswith('[User sent') for i in agent.interrupts] == [True]
    assert text is not None and '"paid words"' in text and 'different words' not in text
    assert forwarded_text is not None and '"different words"' in forwarded_text and 'paid words' not in forwarded_text
    assert len(calls) == 2
    assert _echoes(sent) == [(CHAT, '🎙️ "paid words"'), (other.chat_id, '🎙️ "different words"')]


@pytest.mark.asyncio
async def test_a_clip_listed_twice_in_one_message_echoes_once(tmp_path, monkeypatch):
    calls = []

    def transcribe_audio(path, model=None, source=None):
        calls.append(path)
        return {'success': True, 'transcript': 'twice', 'provider': 'f'}

    monkeypatch.setattr('tools.transcription_tools.transcribe_audio', transcribe_audio)
    runner, authority, source, _agent, sent = await _busy_rig(tmp_path, monkeypatch)
    clip = tmp_path / 'note.ogg'
    clip.write_bytes(b'OggS current and quoted media')
    try:
        event = MessageEvent(text='', message_type=MessageType.VOICE, source=source, message_id='d1',
                             media_urls=[str(clip), str(clip)], media_types=['audio/ogg', 'audio/ogg'])
        await runner._prepare_inbound_message_text(event=event, source=source, history=[])

        assert len(calls) == 1
        assert _echoes(sent) == [(CHAT, '🎙️ "twice"')]
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_a_refused_busy_echo_is_sent_by_the_turn(tmp_path, monkeypatch):
    """Flood control refusing the busy path's echo must not mark the clip shown: the FIFO turn echoes it."""
    monkeypatch.setattr('tools.transcription_tools.transcribe_audio',
                        lambda path, model=None, source=None: {'success': True, 'transcript': 'hi', 'provider': 'f'})
    runner, authority, source, _agent, _sent = await _busy_rig(tmp_path, monkeypatch)
    shown, refuse, attempts = [], [True], []

    async def send(chat_id, content, **_):
        if '"hi"' in content:
            attempts.append(content)
            if refuse[0]:
                return SendResult(success=False, error='Flood control exceeded')
        shown.append(content)
        return SendResult(success=True, message_id='s')

    monkeypatch.setattr(runner.adapters[Platform.WHATSAPP], 'send', send)
    clip = tmp_path / 'note.ogg'
    clip.write_bytes(b'OggS refused echo')
    try:
        async with _admit_busy_voice(runner, authority, source, clip) as restored:
            await async_wait_until(lambda: attempts, timeout=10, what='the refused prefetch echo')
            assert not any('"hi"' in m for m in shown)
            refuse[0] = False
            await runner._prepare_inbound_message_text(event=restored, source=restored.source, history=[])

        assert attempts == ['🎙️ "hi"'] * 2
        assert [m for m in shown if '"hi"' in m] == ['🎙️ "hi"']
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_a_partly_refused_busy_echo_repeats_no_shown_clip(tmp_path, monkeypatch):
    """The admitted turn retries only the middle clip whose prefetch echo was refused."""
    words = {'a.ogg': 'one', 'b.ogg': 'two', 'c.ogg': 'three'}
    monkeypatch.setattr('tools.transcription_tools.transcribe_audio',
                        lambda path, model=None, source=None: {
                            'success': True, 'transcript': words[os.path.basename(path)], 'provider': 'f'})
    runner, authority, source, _agent, _sent = await _busy_rig(tmp_path, monkeypatch)
    shown, refuse = [], [True]

    async def send(chat_id, content, **_):
        if refuse[0] and '"two"' in content:
            return SendResult(success=False, error='Flood control exceeded')
        shown.append(content)
        return SendResult(success=True, message_id='s')

    monkeypatch.setattr(runner.adapters[Platform.WHATSAPP], 'send', send)
    clips = []
    for name in words:
        clips.append(tmp_path / name)
        clips[-1].write_bytes(b'OggS ' + name.encode())
    def echoes():
        return [m for m in shown if m.startswith('🎙️')]

    try:
        async with _admit_busy_voice(runner, authority, source, *clips) as restored:
            await async_wait_until(lambda: len(echoes()) == 2, timeout=10, what='accepted prefetch echoes')
            assert echoes() == ['🎙️ "one"', '🎙️ "three"']
            refuse[0] = False
            await runner._prepare_inbound_message_text(event=restored, source=restored.source, history=[])

        assert echoes() == ['🎙️ "one"', '🎙️ "three"', '🎙️ "two"']
    finally:
        authority.db.close()
