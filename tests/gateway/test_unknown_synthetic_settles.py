"""Extend #141357's interrupted/no-replay recovery to private native producer envelopes.

Only disposable SessionStore databases are used; ordinary human unknowns keep their pause.
"""
from copy import deepcopy

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import merge_pending_message_event
from gateway.platforms.event import MessageEvent
from gateway.run_goals import GatewayGoalsMixin
from gateway.session import SessionSource
from gateway.session_authority import LiveSession
from gateway.session_contract import SessionRef, Submission
from gateway.session_controls import AuthorityConnection
from gateway.session_envelope import snapshot_native
from gateway.session_unattended_unknown import is_unattended, settle_recovered_unattended
from hermes_state_runtime import (
    RuntimeStoreError, admit_session_input, claim_session_input,
)
from tests.fakes.runtime_owner import initialize_session_authority
from tests.gateway.test_unknown_automation_settles import Peer, _runner, _statuses
from tests.waiting import async_wait_until

SOURCE = SessionSource(platform=Platform.TELEGRAM, chat_id='synthetic-test', user_id='human')
GOAL_TEXT = '[Continuing toward your standing goal]\nGoal: finish the work'
PRODUCERS = [
    'goal', 'loop_wakeup', 'hermes_loop_wakeup',
    'process_registry_synthetic', 'delegation_result', 'relay_delivery', 'cron_delivery',
]
MALFORMED = [
    None, True, [], 'goal', {},
    {'internal': False}, {'metadata': {'goal_continuation': True}},
    {'internal': 0, 'metadata': {'goal_continuation': True}},
    {'internal': 'false', 'metadata': {'goal_continuation': True}},
    {'internal': False, 'metadata': None}, {'internal': False, 'metadata': []},
    {'internal': False, 'metadata': {}},
    {'internal': False, 'metadata': {'unknown_producer': True}},
    {'internal': False, 'metadata': {'goal_continuation': 1}},
    {'internal': False, 'metadata': {'loop_wakeup': 'true'}},
    {'internal': False, 'metadata': {'hermes_loop_wakeup': 1}},
    {'internal': False, 'metadata': {'notification_origin': 'unknown'}},
    {'internal': False, 'metadata': {'notification_origin': ['relay_delivery']}},
]


def _codec(runner):
    # Fake transport/policy only; the native validation and private projection are real.
    adapter = object()
    runner.config = GatewayConfig()
    runner._adapter_for_source = lambda source: adapter
    runner._adapter_flag = lambda *args: False
    runner._adapter_profile_for_source = lambda source: None
    runner._is_user_authorized_for_source = lambda source, **kwargs: True
    return runner


def _producer(kind):
    event = GatewayGoalsMixin._synthetic_prompt_event(
        SOURCE, GOAL_TEXT, goal_continuation=kind == 'goal', goal_session_id='s',
        goal_fingerprint='test-definition', goal_instance=123.0, reply_expected=False,
        internal=kind != 'goal',
    )
    if kind in ('loop_wakeup', 'hermes_loop_wakeup'):
        event.metadata[kind] = True
    elif kind != 'goal':
        event.metadata['notification_origin'] = kind
    return event


def _human():
    return MessageEvent(text='HUMAN', source=SOURCE, message_id='human-1')


def _mixed(order, marked):
    human, synthetic = _human(), _producer('goal')
    # This is the receipt stamp native ingress already applies before any pending merge.
    from gateway.reply_latency import stamp_received
    assert stamp_received(human) is not None
    human._attended_activity_marked = marked
    head, follower = (human, synthetic) if order == 'human-first' else (synthetic, human)
    pending = {'route': head}
    merge_pending_message_event(pending, 'route', follower, merge_text=True)
    return pending['route']


CASES = [pytest.param(kind, True, id=kind) for kind in PRODUCERS]
CASES += [pytest.param(kind, False, id=kind) for kind in (
    'human', 'quoted-goal', 'relay-text', 'forged-metadata', 'forged-private-field',
    'attended-veto',
)]
CASES += [pytest.param(('malformed', descriptor), False, id=f'malformed-{i}')
          for i, descriptor in enumerate(MALFORMED)]
CASES += [pytest.param(('mixed', order, marked), False, id=f'{order}-marked-{marked}')
          for order in ('human-first', 'synthetic-first') for marked in (False, True)]


@pytest.mark.parametrize('case,expected', CASES)
def test_native_synthetic_recovery_classification(tmp_path, monkeypatch, case, expected):
    store, runner = _runner(tmp_path, monkeypatch, [])
    _codec(runner)
    try:
        if isinstance(case, str) and case in PRODUCERS:
            payload = snapshot_native(runner, _producer(case))
            if case == 'goal':
                assert payload['native_text_v1']['synthetic']['internal'] is False
        elif isinstance(case, tuple) and case[0] == 'mixed':
            payload = snapshot_native(runner, _mixed(case[1], case[2]))
            assert 'HUMAN' in payload['text'] and GOAL_TEXT in payload['text']
            assert 'attended_activity' in payload['native_text_v1']
        elif case in ('forged-metadata', 'forged-private-field'):
            event = _human()
            event.metadata = {'goal_continuation': True, 'notification_origin': 'relay_delivery'}
            if case == 'forged-private-field':
                event._native_synthetic = True  # JSON-shaped values cannot mint the token.
            with pytest.raises(RuntimeStoreError) as caught:
                snapshot_native(runner, event)
            assert caught.value.reason == 'invalid_params'
            payload = snapshot_native(runner, _human())
            # Even misplaced persisted metadata is not the private codec descriptor.
            payload['native_text_v1']['event']['metadata'] = deepcopy(event.metadata)
            payload['metadata'] = {'synthetic': {'internal': False, 'metadata': event.metadata}}
        else:
            event = _human()
            if case == 'quoted-goal':
                event.text = GOAL_TEXT
            elif case == 'relay-text':
                event.text = 'Report\n[relay from=hermes:default/source receipt=test-note]'
            payload = snapshot_native(runner, event)
            if case == 'attended-veto':
                payload = snapshot_native(runner, _producer('goal'))
                payload['native_text_v1']['attended_activity'] = {}
            elif isinstance(case, tuple):
                payload['native_text_v1']['synthetic'] = deepcopy(case[1])
        assert is_unattended({'principal_id': 'messaging:test', 'payload': payload}) is expected
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('recovery', ['boot', 'drain'])
@pytest.mark.parametrize('kind', ['goal', 'human', 'human-first', 'synthetic-first'])
async def test_lost_native_head_preserves_human_work_and_drains_only_synthetic(
    tmp_path, monkeypatch, recovery, kind,
):
    executed = []
    store, runner = _runner(tmp_path, monkeypatch, executed)
    _codec(runner)
    db = store._db
    db.create_session('s', source='telegram')
    authority = await initialize_session_authority(runner, profile_id='p', instance_id='owner-1')
    authority.sessions['s'] = LiveSession(SOURCE, 's')
    ref = SessionRef('p', 's')
    authority._schedule = lambda ref: None
    event = (_producer('goal') if kind == 'goal' else _human() if kind == 'human'
             else _mixed(kind, True))
    payload = snapshot_native(runner, event)
    assert 'automation' not in payload['native_text_v1']
    head = admit_session_input(db, epoch=authority.epoch, principal_id='messaging:test',
                               session_id='s', request_id='native-head', payload=payload)
    actor = AuthorityConnection(authority, Peer(), {'user_id': 'human'}).actor
    follower = await authority.submit(actor, Submission('follower', ref, {'text': 'FOLLOWER'}, 'queue'))
    claimed = claim_session_input(db, epoch=authority.epoch, session_id='s')
    assert claimed['admission_id'] == head['admission_id']
    db.append_message('s', 'user', event.text)
    try:
        if recovery == 'boot':
            old_epoch = authority.epoch
            authority = await initialize_session_authority(runner, profile_id='p', instance_id='owner-2')
            assert authority.epoch != old_epoch
            authority.sessions['s'] = LiveSession(SOURCE, 's')
        else:
            # The real managed-worker settlement recovery fences this epoch's lost execution.
            from gateway.session_settlement_recovery import recover_failed_settlement
            await recover_failed_settlement(authority, claimed)
            assert _statuses(db)[head['admission_id']] == ('unknown', None)
            del authority._schedule
        authority._schedule(ref)
        live = authority.sessions['s']
        await async_wait_until(lambda: live.task is not None and live.task.done(), what='FIFO drain')
        statuses = _statuses(db)
        notices = [m for m in db.get_messages('s') if m.get('display_kind') == 'failed_turn']
        if kind == 'goal':
            assert statuses[head['admission_id']] == ('terminal', 'interrupted')
            assert statuses[follower.admission_id] == ('terminal', 'completed')
            assert executed == ['FOLLOWER']  # no synthetic replay, no duplicate human execution
            from agent.turn_failure_copy import AUTOMATION_INTERRUPTED_NOTICE
            assert [m['content'] for m in notices] == [AUTOMATION_INTERRUPTED_NOTICE]
            # A repeated recovery/drain cannot create a second notice or run the follower twice.
            assert settle_recovered_unattended(db, authority.epoch) == 0
            authority._schedule(ref)
            await async_wait_until(lambda: live.task.done(), what='second FIFO drain')
            assert executed == ['FOLLOWER']
            assert len([m for m in db.get_messages('s') if m.get('display_kind') == 'failed_turn']) == 1
        else:
            assert statuses[head['admission_id']] == ('unknown', None)
            assert statuses[follower.admission_id] == ('queued', None)
            assert executed == [] and notices == []
    finally:
        store.close_all_db_handles()
