"""The authority's busy preempt keeps main's busy handler: voice steers, steer folds, busy ack."""
import asyncio

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session_authority import SessionAuthority
from tests.fakes.runtime_owner import initialize_session_authority
from hermes_state_runtime import list_session_admissions

CHAT = '15551234567@s.whatsapp.net'


class _RunningAgent:
    def __init__(self):
        self.steers, self.interrupts = [], []

    def steer(self, text, *, human=True):
        self.steers.append(text)
        return True

    def interrupt(self, *args, **kwargs):
        self.interrupts.append((args, kwargs))

    def get_activity_summary(self):
        return {}


async def _setup(tmp_path, monkeypatch, mode):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
    monkeypatch.setenv('WHATSAPP_ALLOWED_USERS', '15551234567')
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={'session_path': str(tmp_path / 'wa')}))
    adapter.gateway_runner, runner.adapters = runner, {Platform.WHATSAPP: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='busy')
    runner._wire_adapter_handlers(adapter)
    sent = []

    async def send(chat_id, content, **_):
        sent.append(content)
        return SendResult(success=True, message_id='s')

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_effective_busy_input_mode', lambda source: mode)
    source = adapter.build_source(chat_id=CHAT, user_id=CHAT, chat_type='group')
    key = runner._session_key_for_source(source)
    agent = _RunningAgent()
    turn = runner._session_state(key).turn
    turn.agent = agent
    turn.event = MessageEvent(text='unaddressed chatter', source=source, message_id='t0', reply_expected=False)
    return runner, authority, source, agent, sent, turn


def _admitted(authority):
    return [row for sid in authority.sessions
            for row in list_session_admissions(authority.db, session_id=sid, pending_only=False)]


@pytest.mark.asyncio
async def test_busy_voice_note_steers_with_transcript_folds_and_acks(tmp_path, monkeypatch):
    runner, authority, source, agent, sent, turn = await _setup(tmp_path, monkeypatch, 'steer')

    async def transcribe(event, adapter, source, text, **_):
        return 'TRANSCRIBED voice follow-up', ['TRANSCRIBED voice follow-up']

    monkeypatch.setattr(runner, '_transcribe_and_echo_pending_voice', transcribe)
    monkeypatch.setattr(runner, '_pending_event_audio_paths', lambda event: list(event.media_urls))
    voice = tmp_path / 'note.ogg'
    voice.write_bytes(b'OggS')
    try:
        handled = asyncio.ensure_future(runner._handle_message(MessageEvent(
            text='', message_type=MessageType.VOICE, source=source, message_id='v1', reply_expected=True,
            media_urls=[str(voice)], media_types=['audio/ogg'])))
        done, _ = await asyncio.wait({handled}, timeout=10)
        handled.cancel()
        assert done, 'the voice note was admitted as a turn behind the running one instead of steering'
        assert len(agent.steers) == 1 and agent.steers[0].endswith('TRANSCRIBED voice follow-up'), agent.steers
        assert turn.event.reply_expected is True  # addressed steer folded: a silence marker can't end it
        assert sent, 'no busy ack'
        assert _admitted(authority) == []  # steered text is inside the run, never replayed
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_busy_voice_note_interrupts_with_transcript_in_interrupt_mode(tmp_path, monkeypatch):
    runner, authority, source, agent, sent, _turn = await _setup(tmp_path, monkeypatch, 'interrupt')

    async def transcribe(event, adapter, source, text, **_):
        return 'TRANSCRIBED stop that', ['TRANSCRIBED stop that']

    monkeypatch.setattr(runner, '_transcribe_and_echo_pending_voice', transcribe)
    monkeypatch.setattr(runner, '_pending_event_audio_paths', lambda event: list(event.media_urls))
    monkeypatch.setattr(runner, '_handle_message_with_agent', lambda *a, **k: None)
    voice = tmp_path / 'note.ogg'
    voice.write_bytes(b'OggS')
    try:
        task = asyncio.ensure_future(runner._handle_message(MessageEvent(
            text='', message_type=MessageType.VOICE, source=source, message_id='v2',
            media_urls=[str(voice)], media_types=['audio/ogg'])))
        from tests.waiting import async_wait_until
        await async_wait_until(lambda: _admitted(authority), timeout=10, what='the voice note admitted')
        task.cancel()
        assert agent.interrupts == [(('TRANSCRIBED stop that',), {})], agent.interrupts
        assert sent, 'no busy ack'
    finally:
        authority.db.close()


def _goal_turn(runner, source, turn, agent):
    """The running turn is an unattended goal continuation, as after a restart's goal wake."""
    turn.agent = agent
    turn.event = runner._synthetic_prompt_event(
        source, 'Continue working toward the goal.', reply_expected=False, goal_continuation=True,
        goal_session_id='goal-session', goal_instance=1.0)


async def _queued_behind(tmp_path, monkeypatch, opener, message_id, *, publish_pending=False, wait_for_busy=True,
                    active_subagents=False):
    """Send a typed queue-mode message behind ``opener`` and return its admitted rows, steers and sends."""
    from tests.waiting import async_wait_until
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner, authority, source, agent, sent, turn = await _setup(tmp_path, monkeypatch, 'queue')
    monkeypatch.setattr(runner, '_effective_busy_text_mode', lambda source: 'queue')
    monkeypatch.setattr(runner, '_handle_message_with_agent', lambda *a, **k: None)
    if active_subagents:
        monkeypatch.setattr(runner, '_agent_has_active_subagents', lambda _agent: True)
    opener(runner, source, turn, agent)
    if publish_pending:
        turn.agent = _AGENT_PENDING_SENTINEL
        turn.agent_published_event = asyncio.Event()
        turn.turn_ended_event = asyncio.Event()
    try:
        event = MessageEvent(
            text='check now', source=source, message_id=message_id, reply_expected=True,
        )
        task = asyncio.ensure_future(runner._hm_admit_to_fifo(
            authority, event, source, runner._session_key_for_source(source), True,
        ))
        await async_wait_until(lambda: _admitted(authority), timeout=10, what='the typed message admitted')
        if publish_pending:
            turn.agent_published_generation = runner._current_session_run_generation(runner._session_key_for_source(source))
            turn.agent_published_agent = agent
            turn.agent = agent
            turn.agent_published_event.set()
        if wait_for_busy:
            await async_wait_until(
                lambda: agent.interrupts or any('Queued for the next turn' in str(x) for x in sent),
                timeout=10, what='the busy result',
            )
        task.cancel()
        return _admitted(authority), agent.steers, agent.interrupts, sent
    finally:
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('starting', [False, True], ids=['published', 'pending-then-published'])
async def test_typed_text_behind_machine_turn_is_soft_preempted_once(tmp_path, monkeypatch, starting):
    """A queued row is preserved while the authority-owned machine turn is cooperatively cut."""
    from gateway.run import _AGENT_PENDING_SENTINEL

    def opener(runner, source, turn, agent):
        _goal_turn(runner, source, turn, _AGENT_PENDING_SENTINEL if starting else agent)

    rows, steers, interrupts, sent = await _queued_behind(
        tmp_path, monkeypatch, opener, 'h1', publish_pending=starting,
    )
    assert [(row['request_id'], row['status']) for row in rows] == [('h1', 'queued')]
    assert steers == []
    assert interrupts == [( (None,), {'tool_reason': 'gateway user turn preemption'})], interrupts
    assert len(sent) == 1 and 'Interrupting' in str(sent[0]), sent


@pytest.mark.asyncio
async def test_typed_text_behind_machine_turn_with_active_subagents_stays_queued(tmp_path, monkeypatch):
    def opener(runner, source, turn, agent):
        _goal_turn(runner, source, turn, agent)

    rows, steers, interrupts, sent = await _queued_behind(
        tmp_path, monkeypatch, opener, 'h-sub', active_subagents=True, wait_for_busy=False,
    )
    assert [(row['request_id'], row['status']) for row in rows] == [('h-sub', 'queued')]
    assert interrupts == [], interrupts
    assert len(sent) == 1 and 'queued for when it finishes' in str(sent[0]).lower(), sent


@pytest.mark.asyncio
async def test_agent_origin_text_behind_machine_turn_does_not_preempt(tmp_path, monkeypatch):
    def opener(runner, source, turn, agent):
        _goal_turn(runner, source, turn, agent)

    runner, authority, source, agent, sent, turn = await _setup(tmp_path, monkeypatch, 'queue')
    monkeypatch.setattr(runner, '_effective_busy_text_mode', lambda source: 'queue')
    try:
        event = MessageEvent(
            text='[relay from=agent:other receipt=r1] continue this',
            source=source, message_id='h-agent-origin', reply_expected=True,
        )
        _goal_turn(runner, source, turn, agent)
        assert not await runner._hm_busy_preempt(event, source, runner._session_key_for_source(source))
        assert agent.interrupts == []
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_relay_event_behind_machine_turn_does_not_preempt(tmp_path, monkeypatch):
    runner, authority, source, agent, sent, turn = await _setup(tmp_path, monkeypatch, 'queue')
    monkeypatch.setattr(runner, '_effective_busy_text_mode', lambda source: 'queue')
    try:
        _goal_turn(runner, source, turn, agent)
        event = MessageEvent(
            text='relay payload', source=source, message_id='h-relay', internal=True,
            reply_expected=False,
        )
        assert not await runner._hm_busy_preempt(event, source, runner._session_key_for_source(source))
        assert agent.interrupts == []
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_typed_text_behind_a_persons_own_turn_still_queues_silently(tmp_path, monkeypatch):
    def opener(runner, source, turn, agent):
        turn.event = MessageEvent(text='do the thing', source=source, message_id='t1', reply_expected=True)
    rows, steers, interrupts, sent = await _queued_behind(
        tmp_path, monkeypatch, opener, 'h2',
    )
    assert [(row['request_id'], row['status']) for row in rows] == [('h2', 'queued')]
    assert steers == [] and interrupts == []
    assert len(sent) == 1 and 'Queued for the next turn' in str(sent[0]), sent
