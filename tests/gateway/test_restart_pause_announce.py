"""A chat paused by startup recovery is told so without having to write again (GW-10 follow-up).

A forced restart fenced a Telegram turn ``unknown``; the person's messages queued behind it never
ran and nobody told them, because the pause notice only rode a NEW message's reply. Real runner
dispatch, real SessionDB and SessionAuthority recovery; only the transport is stubbed."""
import asyncio

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.run_runtime import recover_profile_native_sessions
from gateway.session_ingress_context import native_callback, register_transport_home
from hermes_constants import get_hermes_home
from hermes_state_runtime import admit_session_input, claim_session_input, get_session_admission
from tests.fakes.runtime_owner import initialize_session_authority
from tests.gateway.test_command_bypass_active_session import _StubAdapter

PAUSED = 'This conversation is paused'


def _gateway(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '42')
    monkeypatch.setattr('gateway.run._hermes_home', tmp_path)
    monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', tmp_path / 'state.db')
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=False, sessions_dir=tmp_path / 'sessions'))
    adapter = _StubAdapter(PlatformConfig(enabled=True, token='fixture', typing_indicator=False), Platform.TELEGRAM)
    sent = []

    async def send(chat_id, content, **kwargs):
        sent.append((chat_id, content, kwargs.get('metadata') or {}))
        return SendResult(success=True, message_id=str(len(sent)))
    monkeypatch.setattr(adapter, 'send', send)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._wire_adapter_handlers(adapter)
    register_transport_home(runner, None, tmp_path)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    return runner, adapter, source, sent


async def _settle(authority, adapter=None):
    """Wait out the unprompted notice and (for an inbound message) the adapter's reply task."""
    for _ in range(3):
        pending = [t for t in getattr(authority, 'pause_announcements', {}).values() if not t.done()]
        if adapter is not None:
            pending += list(adapter._background_tasks)
        live = [s.task for s in authority.sessions.values() if s.task is not None and not s.task.done()]
        if not pending and not live:
            return
        await asyncio.wait_for(asyncio.gather(*pending, *live, return_exceptions=True), 10)


def _notices(sent):
    return [content for _chat, content, _meta in sent if PAUSED in content]


async def _lose_human_turn_behind_queued_message(runner, source, home):
    """The old owner admitted the person's message, claimed it, and admitted a second one behind it."""
    first = await initialize_session_authority(runner, profile_id=str(home), instance_id='old')
    first._schedule = lambda ref: None
    head = MessageEvent(text='lost human input', source=source, message_id='head')
    with native_callback(runner, head, get_hermes_home()):
        receipt = await first.admit_native(head)
    sid = receipt.ref.session_id
    claimed = claim_session_input(first.db, epoch=first.epoch, session_id=sid)
    follower = MessageEvent(text='queued human follow-up', source=source, message_id='follower')
    with native_callback(runner, follower, get_hermes_home()):
        queued = await first.admit_native(follower)
    return sid, claimed['admission_id'], queued.admission_id


async def _restart(runner, home):
    recovered = await initialize_session_authority(runner, profile_id=str(home), instance_id='restarted')
    verdicts = await recover_profile_native_sessions(runner, recovered)
    return recovered, verdicts


@pytest.mark.asyncio
async def test_restart_pause_is_announced_once_and_discard_still_resumes(tmp_path, monkeypatch):
    runner, adapter, source, sent = _gateway(tmp_path, monkeypatch)
    try:
        sid, lost, queued = await _lose_human_turn_behind_queued_message(runner, source, tmp_path)
        recovered, verdicts = await _restart(runner, tmp_path)
        assert verdicts[sid] == 'unknown_execution'
        await _settle(recovered)

        # (a) No new inbound message: the chat is still told, exactly once, in its own chat.
        assert len(_notices(sent)) == 1, sent
        chat_id, notice, metadata = sent[-1]
        assert chat_id == '42' and '/discard' in notice
        assert metadata.get('_interim_send') is True  # never sealed as some turn's answer
        assert 'lost human input' not in notice and 'follow-up' not in notice  # generic text only
        assert recovered.sessions[sid].pause_notified is True

        # (b) A later message lands on the same pause episode: saved silently, no second notice.
        await adapter.handle_message(MessageEvent(text='hello?', source=source, message_id='later'))
        await _settle(recovered, adapter)
        assert len(_notices(sent)) == 1, sent
        assert get_session_admission(recovered.db, admission_id=queued)['status'] == 'queued'

        # (d) /discard still drops the lost turn and lets the saved messages go.
        scheduled = []
        recovered._schedule = lambda ref: scheduled.append(ref.session_id)
        await adapter.handle_message(MessageEvent(text='/discard', source=source, message_id='discard'))
        await _settle(recovered, adapter)
        row = get_session_admission(recovered.db, admission_id=lost)
        assert (row['status'], row['outcome']) == ('terminal', 'interrupted'), (row, sent)
        assert get_session_admission(recovered.db, admission_id=queued)['status'] == 'queued'
        assert sid in scheduled
        assert len(_notices(sent)) == 1, sent
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_a_later_restart_does_not_announce_the_same_pause_again(tmp_path, monkeypatch):
    """(e) The episode outlives the process: a pause an earlier boot already announced is not told
    to the chat again by every later restart while the person has not answered it yet."""
    runner, _adapter, source, sent = _gateway(tmp_path, monkeypatch)
    try:
        sid, lost, queued = await _lose_human_turn_behind_queued_message(runner, source, tmp_path)
        first, verdicts = await _restart(runner, tmp_path)
        assert verdicts[sid] == 'unknown_execution'
        await _settle(first)
        assert len(_notices(sent)) == 1, sent
        for instance in ('restarted-again', 'restarted-third'):
            again = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id=instance)
            verdicts = await recover_profile_native_sessions(runner, again)
            assert verdicts[sid] == 'unknown_execution'  # still paused for the person to resolve
            await _settle(again)
            assert len(_notices(sent)) == 1, sent
        assert get_session_admission(again.db, admission_id=lost)['status'] == 'unknown'
        assert get_session_admission(again.db, admission_id=queued)['status'] == 'queued'
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('told_by', ['next_boot', 'reply'])
async def test_a_notice_that_never_reached_the_chat_is_not_lost_to_a_restart(tmp_path, monkeypatch, told_by):
    """(f) A boot whose unprompted send failed leaves the episode untold: the next boot still
    announces it once. A reply that carried the notice meanwhile counts as told for later boots."""
    runner, adapter, source, sent = _gateway(tmp_path, monkeypatch)
    deliver, failed = adapter.send, []

    async def flaky(chat_id, content, **kwargs):
        if PAUSED in content and not failed:
            failed.append(content)
            return SendResult(success=False, error='network down')
        return await deliver(chat_id, content, **kwargs)
    monkeypatch.setattr(adapter, 'send', flaky)
    try:
        sid, lost, queued = await _lose_human_turn_behind_queued_message(runner, source, tmp_path)
        first, verdicts = await _restart(runner, tmp_path)
        assert verdicts[sid] == 'unknown_execution'
        await _settle(first)
        assert len(failed) == 1 and _notices(sent) == [], sent
        expected = 1
        if told_by == 'reply':
            await adapter.handle_message(MessageEvent(text='hello?', source=source, message_id='later'))
            await _settle(first, adapter)
            await asyncio.gather(*getattr(first, '_mutation_tasks', ()), return_exceptions=True)
            assert len(_notices(sent)) == 1, sent  # the reply carried it
        for instance in ('restarted-again', 'restarted-third'):
            again = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id=instance)
            verdicts = await recover_profile_native_sessions(runner, again)
            assert verdicts[sid] == 'unknown_execution'
            await _settle(again)
            assert len(_notices(sent)) == expected, sent  # told exactly once across every boot
        assert get_session_admission(again.db, admission_id=lost)['status'] == 'unknown'
        assert get_session_admission(again.db, admission_id=queued)['status'] == 'queued'
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('told_by', ['next_message', 'next_boot'])
async def test_a_reply_notice_that_failed_to_send_is_not_recorded_as_told(tmp_path, monkeypatch, told_by):
    """(g) The unprompted notice failed, and so did the reply that then carried it: the chat was
    never told, so neither the next message on this boot nor the next boot may stay silent."""
    runner, adapter, source, sent = _gateway(tmp_path, monkeypatch)
    deliver, failed, down = adapter.send, [], [True]

    async def flaky(chat_id, content, **kwargs):
        if PAUSED in content and down:
            failed.append(content)
            return SendResult(success=False, error='network down')
        return await deliver(chat_id, content, **kwargs)
    monkeypatch.setattr(adapter, 'send', flaky)
    try:
        sid, lost, queued = await _lose_human_turn_behind_queued_message(runner, source, tmp_path)
        first, verdicts = await _restart(runner, tmp_path)
        assert verdicts[sid] == 'unknown_execution'
        await _settle(first)
        await adapter.handle_message(MessageEvent(text='hello?', source=source, message_id='later'))
        await _settle(first, adapter)
        await asyncio.gather(*getattr(first, '_mutation_tasks', ()), return_exceptions=True)
        assert len(failed) >= 2 and _notices(sent) == [], (failed, sent)  # the announcement and the reply
        down.clear()
        if told_by == 'next_message':
            await adapter.handle_message(MessageEvent(text='anyone?', source=source, message_id='again'))
            await _settle(first, adapter)
            assert len(_notices(sent)) == 1, sent
        else:
            again = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='restarted-again')
            verdicts = await recover_profile_native_sessions(runner, again)
            assert verdicts[sid] == 'unknown_execution'
            await _settle(again)
            assert len(_notices(sent)) == 1, sent
        assert get_session_admission(first.db, admission_id=lost)['status'] == 'unknown'
        assert get_session_admission(first.db, admission_id=queued)['status'] == 'queued'
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('announcement', ['fails', 'delivered'])
async def test_a_message_during_the_unprompted_send_is_not_left_silent(tmp_path, monkeypatch, announcement):
    """(h) A message lands while the unprompted notice is still being sent. Delivered: that was the
    episode's one notice and the reply stays silent. Failed: the reply carries it instead, so the
    chat is not left untold by both paths."""
    from gateway.session_authority import SessionAuthority
    runner, adapter, source, sent = _gateway(tmp_path, monkeypatch)
    deliver, entered, gate, failed = adapter.send, asyncio.Event(), asyncio.Event(), []

    async def slow(chat_id, content, **kwargs):
        if PAUSED in content and not entered.is_set():
            entered.set()
            await gate.wait()
            if announcement == 'fails':
                failed.append(content)
                return SendResult(success=False, error='network down')
        return await deliver(chat_id, content, **kwargs)
    monkeypatch.setattr(adapter, 'send', slow)
    released, pause = asyncio.Event(), SessionAuthority._pause

    def observed_pause(self, ref, reason):
        pause(self, ref, reason)
        if entered.is_set():
            released.set()  # the new message's waiter was just released onto the reply path
    monkeypatch.setattr(SessionAuthority, '_pause', observed_pause)
    try:
        sid, lost, queued = await _lose_human_turn_behind_queued_message(runner, source, tmp_path)
        first, verdicts = await _restart(runner, tmp_path)
        assert verdicts[sid] == 'unknown_execution'
        await asyncio.wait_for(entered.wait(), 10)  # the unprompted notice is in flight
        await adapter.handle_message(MessageEvent(text='hello?', source=source, message_id='later'))
        await asyncio.wait_for(released.wait(), 10)
        for _ in range(20):
            await asyncio.sleep(0)  # the reply path decides while the send is still in flight
        gate.set()
        await _settle(first, adapter)
        assert len(_notices(sent)) == 1, (failed, sent)
        metadata = next(meta for _chat, content, meta in sent if PAUSED in content)
        assert (metadata.get('_interim_send') is True) is (announcement == 'delivered'), sent
        assert len(failed) == (announcement == 'fails')
        assert get_session_admission(first.db, admission_id=lost)['status'] == 'unknown'
        assert get_session_admission(first.db, admission_id=queued)['status'] == 'queued'
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('boot', ['settled', 'left-unknown'])
async def test_no_notice_when_no_person_is_waiting(tmp_path, monkeypatch, boot):
    """(c) A lost automation turn pages nobody: boot settles it, or (its resolution refused) it
    pauses the FIFO with no person's input behind it."""
    from gateway.run_goals import GatewayGoalsMixin
    from gateway.session_envelope import snapshot_native
    runner, _adapter, source, sent = _gateway(tmp_path, monkeypatch)
    if boot == 'left-unknown':
        monkeypatch.setattr('gateway.session_unattended_unknown.settle_recovered_unattended', lambda db, epoch: 0)
    try:
        first = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='old')
        first._schedule = lambda ref: None
        sid = first.register(source).session_id
        goal = GatewayGoalsMixin._synthetic_prompt_event(
            source, '[Continuing toward your standing goal]\nGoal: finish', goal_continuation=True,
            goal_session_id=sid, goal_fingerprint='definition', goal_instance=1.0, reply_expected=False,
            internal=False)
        head = admit_session_input(first.db, epoch=first.epoch, principal_id='messaging:42', session_id=sid,
                                   request_id='goal-head', payload=snapshot_native(runner, goal))
        claim_session_input(first.db, epoch=first.epoch, session_id=sid)
        recovered, verdicts = await _restart(runner, tmp_path)
        status = get_session_admission(recovered.db, admission_id=head['admission_id'])['status']
        if boot == 'settled':
            assert status == 'terminal'
        else:
            assert status == 'unknown' and verdicts[sid] == 'unknown_execution'
        await _settle(recovered)
        assert _notices(sent) == [], sent
    finally:
        runner.session_store.close_all_db_handles()
