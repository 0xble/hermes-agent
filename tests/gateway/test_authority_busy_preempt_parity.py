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
async def test_busy_voice_note_interrupts_at_once_in_interrupt_mode(tmp_path, monkeypatch):
    """The interrupt carries the media placeholder, never a transcript it waited for (#143961); the
    admitted follow-up keeps the audio and transcribes it as its own turn."""
    runner, authority, source, agent, sent, _turn = await _setup(tmp_path, monkeypatch, 'interrupt')
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
        assert agent.interrupts == [((f'[User sent audio: {voice}]',), {})], agent.interrupts
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
                    active_subagents=False, after_admit=None, message_type=MessageType.TEXT, mode='queue'):
    """Send a typed queue-mode message behind ``opener`` and return its admitted rows, steers and sends."""
    from tests.waiting import async_wait_until
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner, authority, source, agent, sent, turn = await _setup(tmp_path, monkeypatch, mode)
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
            message_type=message_type,
        )
        task = asyncio.ensure_future(runner._hm_admit_to_fifo(
            authority, event, source, runner._session_key_for_source(source), True,
        ))
        await async_wait_until(lambda: _admitted(authority), timeout=10, what='the typed message admitted')
        if after_admit is not None:
            await after_admit(runner, source, turn, agent)
        elif publish_pending:
            turn.publish_agent(agent, None, runner._current_session_run_generation(runner._session_key_for_source(source)))
        if wait_for_busy:
            await async_wait_until(
                lambda: agent.interrupts or any('Queued for the next turn' in str(x) for x in sent),
                timeout=10, what='the busy result',
            )
        await asyncio.wait_for(asyncio.gather(*getattr(runner, '_background_tasks', ()), return_exceptions=True), 10)
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
        tmp_path, monkeypatch, opener, 'h2', wait_for_busy=False,
    )
    assert [(row['request_id'], row['status']) for row in rows] == [('h2', 'queued')]
    assert steers == [] and interrupts == [] and sent == []


@pytest.mark.asyncio
async def test_pending_machine_turn_never_published_gets_one_queued_ack_and_no_interrupt(tmp_path, monkeypatch):
    """The bound expires with no publication: no interrupt, exactly one existing queued ack (as on main)."""
    from gateway.run import _AGENT_PENDING_SENTINEL
    monkeypatch.setattr('gateway.run_turn_lease_wait.lease_wait_max_seconds', lambda: 0.01)

    def opener(runner, source, turn, agent):
        _goal_turn(runner, source, turn, _AGENT_PENDING_SENTINEL)

    async def never_publish(runner, source, turn, agent):
        pass  # the pending sentinel is never replaced and the turn never ends

    rows, steers, interrupts, sent = await _queued_behind(
        tmp_path, monkeypatch, opener, 'h-never', publish_pending=True, after_admit=never_publish,
    )
    assert [(row['request_id'], row['status']) for row in rows] == [('h-never', 'queued')]
    assert interrupts == [] and steers == []
    assert len(sent) == 1 and 'Queued for the next turn' in str(sent[0]), sent


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['steer-mode', 'photo'])
async def test_steer_mode_and_media_keep_queue_semantics_behind_pending_machine_turn(tmp_path, monkeypatch, case):
    """Only typed text in queue mode preempts, including while the agent is still pending."""
    from gateway.run import _AGENT_PENDING_SENTINEL

    def opener(runner, source, turn, agent):
        _goal_turn(runner, source, turn, _AGENT_PENDING_SENTINEL)

    kwargs = {'mode': 'steer'} if case == 'steer-mode' else {'message_type': MessageType.PHOTO}
    rows, steers, interrupts, sent = await _queued_behind(
        tmp_path, monkeypatch, opener, 'h-' + case, publish_pending=True, **kwargs,
    )
    assert [(row['request_id'], row['status']) for row in rows] == [('h-' + case, 'queued')]
    assert interrupts == [] and steers == []
    assert len(sent) == 1 and 'Queued for the next turn' in str(sent[0]), sent


@pytest.mark.asyncio
@pytest.mark.parametrize('successor', ['ended', 'successor-turn', 'generation-bump'])
async def test_pending_wait_ending_or_successor_turn_is_never_interrupted(tmp_path, monkeypatch, successor):
    """The machine turn ends during the pending wait, or a successor reuses the slot: nothing is interrupted."""
    from gateway.run import _AGENT_PENDING_SENTINEL

    def opener(runner, source, turn, agent):
        _goal_turn(runner, source, turn, _AGENT_PENDING_SENTINEL)

    async def end_turn(runner, source, turn, agent):
        key = runner._session_key_for_source(source)
        generation = runner._current_session_run_generation(key)
        if successor == 'successor-turn':
            turn.event = MessageEvent(text='next', source=source, message_id='succ', reply_expected=True)
            turn.publish_agent(agent, None, generation)  # the successor's agent, a different opener
        if successor == 'generation-bump':
            # Same opener event and a real publication, but the run generation moved on (/new, /stop).
            turn.publish_agent(agent, None, runner._begin_session_run_generation(key))
        turn.turn_ended_event.set()

    rows, steers, interrupts, sent = await _queued_behind(
        tmp_path, monkeypatch, opener, 'h-' + successor, publish_pending=True, wait_for_busy=False,
        after_admit=end_turn,
    )
    assert [(row['request_id'], row['status']) for row in rows] == [('h-' + successor, 'queued')]
    assert interrupts == [] and steers == []
    assert sent == []


def test_adopt_agent_keeps_a_soft_preemption_but_still_drops_a_stale_stop(tmp_path, monkeypatch):
    """A preemption seen in the publish-to-adopt gap survives; a stale earlier-generation Stop is dropped."""
    from agent.interrupt_control import InterruptControlMixin
    from tests.gateway.test_session_authority_cancel_settlement import _authority

    class Agent(InterruptControlMixin):
        def __init__(self):
            self._interrupt_requested = False
            self._interrupt_message = self._tool_interrupt_reason = self._interrupt_source = None
            self._pending_steer = self._pending_redirect = None

        def clear_interrupt(self, **_):
            self._interrupt_requested = False
            self._interrupt_message = self._tool_interrupt_reason = self._interrupt_source = None

    db, authority = _authority(tmp_path, monkeypatch)
    with db:
        preempted, stale = Agent(), Agent()
        preempted._interrupt_requested, preempted._tool_interrupt_reason = True, 'gateway user turn preemption'
        preempted._interrupt_source = 'system'
        stale._interrupt_requested, stale._tool_interrupt_reason = True, 'explicit stop requested'
        for agent in (preempted, stale):
            authority.delivered_stops['s'] = 1  # a Stop delivered for an earlier generation
            authority.adopt_agent('s', 2, agent)
        assert preempted._interrupt_requested is True
        assert stale._interrupt_requested is False


@pytest.mark.asyncio
async def test_pending_timeout_ack_is_not_sent_once_the_slot_belongs_to_another_turn(tmp_path, monkeypatch):
    """The bound expires after the opener ended and a successor took the slot: no stale queued ack."""
    from gateway.run import _AGENT_PENDING_SENTINEL
    monkeypatch.setattr('gateway.run_turn_lease_wait.lease_wait_max_seconds', lambda: 2.0)  # long enough to swap the slot first

    def opener(runner, source, turn, agent):
        _goal_turn(runner, source, turn, _AGENT_PENDING_SENTINEL)

    async def successor_takes_slot(runner, source, turn, agent):
        turn.event = MessageEvent(text='next', source=source, message_id='succ-ack', reply_expected=True)

    rows, steers, interrupts, sent = await _queued_behind(
        tmp_path, monkeypatch, opener, 'h-stale-ack', publish_pending=True, wait_for_busy=False,
        after_admit=successor_takes_slot,
    )
    assert [(row['request_id'], row['status']) for row in rows] == [('h-stale-ack', 'queued')]
    assert interrupts == [] and steers == [] and sent == []


class _CachedAgent:
    """A cached agent with the real interrupt-state semantics the next turn's first iteration reads."""

    def __init__(self):
        self._interrupt_requested = False
        self._interrupt_message = self._tool_interrupt_reason = self._interrupt_source = None
        self.api_calls = 0

    def interrupt(self, message=None, *, tool_reason=None, **_):
        self._interrupt_message, self._tool_interrupt_reason = message, tool_reason
        self._interrupt_source = 'system'
        self._interrupt_requested = True

    def clear_interrupt(self, **_):
        self._interrupt_requested = False
        self._interrupt_message = self._tool_interrupt_reason = self._interrupt_source = None

    def run_first_iteration(self):
        """agent/turn_iteration_prep.py: an interrupt pending here breaks out with zero API calls."""
        if self._interrupt_requested:
            return False
        self.api_calls += 1
        return True


def _wire_turn(agent, ctx):
    """The gateway's real per-turn adoption of a (cached) agent by the turn owning *ctx*."""
    from gateway.turn_context import TurnContext
    TurnContext.bind_agent_lifetime(ctx, agent)


def _ctx():
    import types
    return types.SimpleNamespace(agent_holder=[None], _worker=None)


def test_preemption_from_a_previous_generation_is_cleared_when_the_next_turn_adopts_the_agent():
    """A preemption left on the cached agent never interrupts the user's next turn."""
    from gateway.run_turn_pending_preemption import stamp_preemption_target

    agent, machine_ctx, user_ctx = _CachedAgent(), _ctx(), _ctx()
    stamp_preemption_target(agent, machine_ctx)
    agent.interrupt(None, tool_reason='gateway user turn preemption')  # aimed at the machine turn
    _wire_turn(agent, user_ctx)  # generation N+1 adopts the same cached agent
    assert agent._interrupt_requested is False
    assert not hasattr(agent, '_gateway_preempt_ctx')
    assert agent.run_first_iteration() is True and agent.api_calls == 1


def test_preemption_aimed_at_the_adopting_turn_is_kept():
    from gateway.run_turn_pending_preemption import stamp_preemption_target

    agent, ctx = _CachedAgent(), _ctx()
    stamp_preemption_target(agent, ctx)
    agent.interrupt(None, tool_reason='gateway user turn preemption')
    _wire_turn(agent, ctx)
    assert agent._interrupt_requested is True
    assert agent._tool_interrupt_reason == 'gateway user turn preemption'
    assert not hasattr(agent, '_gateway_preempt_ctx')


def test_an_unstamped_user_interrupt_survives_adoption():
    agent = _CachedAgent()
    agent.interrupt('stop that', tool_reason='user interrupt')
    agent._interrupt_source = None
    _wire_turn(agent, _ctx())
    assert agent._interrupt_requested is True


@pytest.mark.asyncio
async def test_preemption_after_the_machine_agent_finished_does_not_interrupt_the_next_user_turn(tmp_path, monkeypatch):
    """The finalizer already cleared interrupts but the slot is not yet released: the late preemption is stale."""
    from tests.waiting import async_wait_until

    runner, authority, source, _agent, sent, turn = await _setup(tmp_path, monkeypatch, 'queue')
    monkeypatch.setattr(runner, '_effective_busy_text_mode', lambda source: 'queue')
    monkeypatch.setattr(runner, '_handle_message_with_agent', lambda *a, **k: None)
    finished, machine_ctx = _CachedAgent(), _ctx()
    _goal_turn(runner, source, turn, finished)
    turn.ctx = machine_ctx  # the machine turn finished its run (finalizer cleared interrupts); slot still held
    try:
        event = MessageEvent(text='check now', source=source, message_id='h-late', reply_expected=True)
        task = asyncio.ensure_future(runner._hm_admit_to_fifo(
            authority, event, source, runner._session_key_for_source(source), True,
        ))
        await async_wait_until(lambda: finished._interrupt_requested, timeout=10, what='the late preemption')
        await asyncio.wait_for(asyncio.gather(*getattr(runner, '_background_tasks', ()), return_exceptions=True), 10)
        task.cancel()
        _wire_turn(finished, _ctx())  # the user's queued turn reuses the cached agent
        assert finished._interrupt_requested is False
        assert finished.run_first_iteration() is True and finished.api_calls == 1
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_agent_published_generation_is_never_none_for_a_pending_waiter():
    """The cold path matches ``agent_published_generation == holder.generation``; None never matches."""
    import types
    from gateway.run_turn import GatewayTurnMixin
    from gateway.session_state import TurnState

    agent, turn = _CachedAgent(), TurnState()
    state = types.SimpleNamespace(turn=turn)
    runner = types.SimpleNamespace(
        _is_session_run_current=lambda key, generation: True,
        _session_state=lambda key: state,
        _current_session_run_generation=lambda key: 7,
        _draining=False,
    )
    ctx = types.SimpleNamespace(session_key='k', run_generation=None, agent_holder=[agent])
    await GatewayTurnMixin._run_agent_track_agent(runner, ctx)
    assert turn.agent is agent and turn.agent_published_generation == 7
