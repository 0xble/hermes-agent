"""A turn the gateway's own restart stopped before its first model call runs once on the successor.

2026-10-31: an update restart interrupted a Telegram turn 11 seconds in, before any model call. The old
process answered "Your message was interrupted before processing started (likely by a recent /stop).
Please send it again." and settled the admission ``interrupted``, so the message was lost and Brian
retyped it. Nothing had run, so the claim now goes back to its FIFO place, the old process sends
nothing, and the next owner answers it. Real runner, adapter, outbox and SQLite; the turn body is stubbed."""
import asyncio

import pytest


def _setup(monkeypatch):
    from gateway.config import GatewayConfig, PlatformConfig, Platform
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from plugins.platforms.telegram.adapter import TelegramAdapter

    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '42')
    config = GatewayConfig()
    config.durable_outbox_enabled = True
    runner = GatewayRunner(config)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    adapter.gateway_runner = runner
    runner.adapters = {Platform.TELEGRAM: adapter}
    monkeypatch.setattr(runner, '_read_user_config', dict)
    monkeypatch.setattr(adapter, '_start_typing_refresh', lambda *a, **kw: None)
    sent = []

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)
        return SendResult(success=True, message_id=f's{len(sent)}')

    monkeypatch.setattr(adapter, 'send', send)
    return runner, adapter, sent


async def _until(predicate, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, 'timed out'
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_restart_before_first_model_call_is_replayed_once_by_the_successor(monkeypatch):
    from gateway.platforms.event import MessageEvent
    from gateway.run import _INTERRUPT_REASON_GATEWAY_RESTART, _normalize_empty_agent_response
    from gateway.run_runtime import recover_profile_native_sessions
    from gateway.session_results import execution_result
    from hermes_state_runtime import list_session_admissions
    from tests.fakes.runtime_owner import initialize_session_authority

    runner, adapter, sent = _setup(monkeypatch)
    turns = []

    async def turn(event, source, quick_key, run_generation):
        turns.append(event._outbox_turn_id)
        if len(turns) == 1:
            # The restart's stop() began while this turn was still building its agent: it is
            # interrupted with the restart reason and has made no model call.
            runner._draining = True
            result = {'final_response': '', 'messages': [], 'interrupted': True, 'api_calls': 0,
                      'interrupt_message': _INTERRUPT_REASON_GATEWAY_RESTART, 'interrupt_source': 'gateway_shutdown'}
            execution_result.get()['result'] = result
            return _normalize_empty_agent_response(result, '')
        result = {'final_response': 'answered ' + event.text, 'messages': [], 'api_calls': 1}
        execution_result.get()['result'] = result
        return result['final_response']

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    old = await initialize_session_authority(runner, profile_id='default', instance_id='old')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42', thread_id='344635')
    key = runner.session_store._generate_session_key(source)
    event = MessageEvent(text='try again', source=source, message_id='m1', platform_update_id=7001)
    await adapter.handle_message(event)
    await adapter._session_tasks[adapter._event_session_key(event)]
    sid = runner.session_store.peek_session_id(key)

    def row():
        rows = list_session_admissions(old.db, session_id=sid, pending_only=False)
        assert len(rows) == 1, rows
        return rows[0]

    await _until(lambda: row()['status'] != 'started')
    assert sent == []  # no "send it again" from the stopping owner
    assert (row()['status'], row()['outcome'], row()['generation']) == ('queued', None, None)

    runner._draining = False
    successor = await initialize_session_authority(runner, profile_id='default', instance_id='successor')
    await recover_profile_native_sessions(runner, successor)
    await _until(lambda: row()['status'] == 'terminal')
    assert (row()['status'], row()['outcome']) == ('terminal', 'completed')
    await _until(lambda: sent)
    assert sent == ['answered try again']
    assert len(turns) == 2 and turns[0] != turns[1]  # the replay is its own outbox turn, not a duplicate


def test_requeue_archives_only_the_unstarted_turn_rows(tmp_path):
    """The replay writes the input once: the stopped turn's user row and its failed-turn boundary
    are archived, earlier history is untouched, and the successor's recovery leaves the row queued."""
    from agent.turn_failure_copy import FAILED_TURN_DISPLAY_KIND
    from hermes_state import SessionDB
    from hermes_state_runtime import (
        admit_session_input, claim_session_input, recover_session_inputs, requeue_unstarted_claim,
    )
    from tests.fakes.runtime_owner import begin_runtime_epoch
    db = SessionDB(tmp_path / 'state.db')
    sid = db.create_session('s1', source='telegram') or 's1'
    db.append_message(sid, 'user', 'earlier')
    db.append_message(sid, 'assistant', 'earlier answer')
    epoch = begin_runtime_epoch(db, instance_id='old')
    admitted = admit_session_input(db, epoch=epoch, principal_id='messaging:x', session_id=sid,
                                   request_id='r1', payload={'text': 'try again'}, intent='queue')
    claimed = claim_session_input(db, epoch=epoch, session_id=sid)
    assert claimed['admission_id'] == admitted['admission_id']
    db.append_message(sid, 'user', 'try again')
    db.append_message(sid, 'assistant', 'Your request was not processed.', display_kind=FAILED_TURN_DISPLAY_KIND)
    assert requeue_unstarted_claim(db, epoch=epoch, admission_id=claimed['admission_id'],
                                   generation=claimed['generation'], transcript_mark=claimed['transcript_mark'])
    assert [m['content'] for m in db.get_messages(sid)] == ['earlier', 'earlier answer']
    successor = begin_runtime_epoch(db, instance_id='new')
    assert recover_session_inputs(db, epoch=successor) == 0
    again = claim_session_input(db, epoch=successor, session_id=sid)
    assert again['admission_id'] == claimed['admission_id'] and again['generation'] > claimed['generation']
    # Only the started generation goes back: an older claim's generation requeues nothing.
    assert not requeue_unstarted_claim(db, epoch=successor, admission_id=claimed['admission_id'],
                                       generation=claimed['generation'])


def test_requeue_keeps_rows_a_bare_input_turn_could_not_have_written(tmp_path):
    """A compaction handoff (or a delivered steer) after the claim means more than the input landed:
    the claim still goes back, and the transcript is left exactly as it is."""
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, claim_session_input, get_session_admission, requeue_unstarted_claim
    from tests.fakes.runtime_owner import begin_runtime_epoch
    db = SessionDB(tmp_path / 'state.db')
    sid = db.create_session('s1', source='telegram') or 's1'
    epoch = begin_runtime_epoch(db, instance_id='old')
    admit_session_input(db, epoch=epoch, principal_id='messaging:x', session_id=sid, request_id='r1',
                        payload={'text': 'try again'}, intent='queue')
    claimed = claim_session_input(db, epoch=epoch, session_id=sid)
    db.append_message(sid, 'user', '[CONTEXT COMPACTION — REFERENCE ONLY] summary', _compressed_summary=True)
    db.append_message(sid, 'user', 'try again')
    assert requeue_unstarted_claim(db, epoch=epoch, admission_id=claimed['admission_id'],
                                   generation=claimed['generation'], transcript_mark=claimed['transcript_mark'])
    assert get_session_admission(db, admission_id=claimed['admission_id'])['status'] == 'queued'
    assert len(db.get_messages(sid)) == 2
