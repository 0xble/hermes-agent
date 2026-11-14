"""Goal waits hand off to the durable FIFO, never to an adapter task receipt."""
import asyncio
import copy
import threading
import time
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_results import execution_result
from hermes_cli import goals
from hermes_state_runtime import RuntimeStoreError, list_session_admissions
from tests.fakes.runtime_owner import initialize_session_authority
from tests.waiting import async_wait_until


@pytest_asyncio.fixture
async def parked(tmp_path, monkeypatch):
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture', typing_indicator=False))
    adapter.gateway_runner, runner.adapters = runner, {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='goal-wait')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', user_id='42', chat_type='dm')
    key = runner._session_key_for_source(source)
    monkeypatch.setattr(adapter, 'send', AsyncMock(return_value=SendResult(success=True, message_id='sent')))
    monkeypatch.setattr(runner, '_handle_message_with_agent', AsyncMock(return_value='seed reply'))
    await adapter.handle_message(MessageEvent(text='seed', source=source, message_id='seed'))
    await async_wait_until(lambda: not adapter._background_tasks, timeout=20, what='seed turn')
    sid = runner.session_store.peek_session_id(key)
    assert sid is not None
    await runner._warm_goals_session_db('goal wait fixture')
    manager = goals.GoalManager(sid)
    manager.set('finish the disposable goal')
    manager.wait_for_seconds(1, 'wait for disposable evidence')
    manager.state.waiting_until = time.time() - 1
    manager.state.last_wait_notice_key = 'unchanged-park-notice'
    manager.state.last_continuation_notice_key = 'unchanged-continuation-notice'
    manager._save()
    # Leave admitted wakes queued so the assertions observe the commit, not a model verdict.
    monkeypatch.setattr(authority, '_schedule', lambda *args: None)
    adapter_calls = AsyncMock(wraps=adapter.handle_message)
    monkeypatch.setattr(adapter, 'handle_message', adapter_calls)

    seed = list_session_admissions(authority.db, session_id=sid, pending_only=False)
    assert len(seed) == 1 and seed[0]['status'] == 'terminal'
    seed_id = seed[0]['admission_id']

    def rows():
        # Inspect the whole disposable ledger: a reset/fork misroute must not hide
        # simply because it committed under the wrong target_session_id.
        from hermes_state_runtime import _row
        with authority.db._read_ctx() as conn:
            return [_row(row) for row in conn.execute(
                'SELECT * FROM session_admissions WHERE admission_id != ? ORDER BY seq',
                (seed_id,)).fetchall()]

    try:
        yield runner, adapter, authority, source, sid, key, manager, rows, adapter_calls
    finally:
        from gateway.session_runtime_workers import mutation_tasks
        adapter_tasks = list(adapter._background_tasks)
        live_tasks = [live.task for live in authority.sessions.values() if live.task]
        for task in adapter_tasks + live_tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*adapter_tasks, *live_tasks, return_exceptions=True), 5)
        while pending := mutation_tasks(authority):
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)
        authority.db.close()
        runner.session_store.close_all_db_handles()


def captured_event(runner, source, sid, manager):
    event = runner._synthetic_prompt_event(source, manager.next_continuation_prompt(),
        reply_expected=False, goal_continuation=True, goal_session_id=sid, goal_state=manager.state)
    event.metadata.update(gateway_session_key=runner._session_key_for_source(source),
        gateway_session_id=sid, gateway_session_strict=True,
        goal_continuation_created_at=manager.state.waiting_since,
        goal_waiting_since=manager.state.waiting_since)
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['not_found', 'queue_full', 'runtime_draining', 'prepare_failure',
                                    'missing_authority', 'missing_provenance', 'suspended'])
async def test_refusal_keeps_exact_wait_then_retry_admits_once(parked, monkeypatch, reason):
    runner, _adapter, authority, _source, sid, key, _manager, rows, adapter_calls = parked
    before = goals.load_goal(sid)
    original = authority.admit_automation

    async def refuse(*args, **kwargs):
        if reason == 'prepare_failure':
            raise ValueError('disposable prepare failure')
        raise RuntimeStoreError(reason if reason in ('queue_full', 'runtime_draining') else 'not_found')

    with monkeypatch.context() as refusal:
        if reason == 'missing_authority':
            refusal.setattr('gateway.session_authorities.active_authority', lambda runner: None)
        elif reason == 'missing_provenance':
            refusal.setattr('gateway.session_automation.committed_route_envelope', lambda *args: None)
        elif reason == 'suspended':
            runner.session_store.lookup_by_session_key(key).suspended = True
        else:
            refusal.setattr(authority, 'admit_automation', refuse)
            # Reproduce the old adapter-created receipt followed by a downstream refusal.
            refusal.setattr(authority, 'admit_native', refuse)
        await asyncio.wait_for(runner._goal_wakeup_fire_one(sid), timeout=5)
        await asyncio.sleep(0)
        # Delivery can wait forever behind our disabled drain; cleanup cancels it.
    runner.session_store.lookup_by_session_key(key).suspended = False
    current = goals.load_goal(sid)
    assert current.waiting_since == before.waiting_since
    assert current.waiting_until == before.waiting_until
    assert current.last_automatic_continuation_at == before.last_automatic_continuation_at
    assert rows() == []
    adapter_calls.assert_not_called()
    assert runner._get_replay_scheduler().gate.load() == 0
    monkeypatch.setattr(authority, 'admit_automation', original)
    # A later tick, after bounded retry backoff, re-resolves the same pinned goal.
    monkeypatch.setattr('gateway.run_goal_wait.monotonic', lambda: 1e12)
    await runner._goal_wakeup_fire_one(sid)
    await runner._goal_wakeup_fire_one(sid)
    assert len(rows()) == 1
    current = goals.load_goal(sid)
    assert current.waiting_since == 0
    assert current.last_automatic_continuation_at > 0
    assert current.last_wait_notice_key == before.last_wait_notice_key
    assert current.last_continuation_notice_key == before.last_continuation_notice_key
    assert runner._get_replay_scheduler().gate.load() == 1


@pytest.mark.asyncio
async def test_private_goal_receipt_is_idempotent_and_metadata_is_not_a_capability(parked):
    from gateway.session_route_wake import admit_route_wake_receipt
    from gateway.run_goal_wait import goal_wait_guard, goal_wait_identity
    from gateway.session_envelope import restore_native

    runner, adapter, authority, source, sid, key, manager, rows, adapter_calls = parked
    event = captured_event(runner, source, sid, manager)
    identity = goal_wait_identity(authority, sid, manager.state)
    guard = goal_wait_guard(authority, event)

    async def admit(item=event, text=None):
        return await admit_route_wake_receipt(runner, adapter, source, text or item.text,
            producer='goal-wait', metadata=item.metadata, event=item,
            identity=identity, _authorize_write=guard)

    first, replay = await asyncio.gather(admit(), admit())
    assert first is not None and replay is not None
    assert first.admission_id == replay.admission_id
    # A separately observed retry has a new wall-clock producer stamp, and may
    # start from a compression successor; neither changes the durable identity.
    child = sid + '-compressed'
    authority.db.publish_compression_child(parent_session_id=sid, child_session_id=child,
        source='discord', messages=[{'role': 'user', 'content': 'summary'}],
        require_compression_lease=False)
    runner.session_store.advance_compression_session(key, sid, child)
    newer = captured_event(runner, source, child, manager)
    newer.metadata['goal_continuation_created_at'] += 100
    assert goal_wait_identity(authority, child, manager.state) == identity
    replay_after_compression = await admit(newer)
    assert replay_after_compression.admission_id == first.admission_id
    assert len(rows()) == 1
    restored = restore_native(rows()[0]['payload'], runner)
    assert restored.internal is False
    assert restored.reply_expected is False
    assert restored.inherit_channel_pin is True
    assert restored.metadata['goal_continuation'] is True
    with pytest.raises(RuntimeStoreError, match='admission_conflict'):
        await admit(text=event.text + ' changed payload')
    forged = MessageEvent(text=event.text, source=source, internal=False,
                          metadata=copy.deepcopy(event.metadata))
    with pytest.raises(RuntimeStoreError):
        await authority.admit_automation(adapter, forged, identity + '-forged')
    assert len(rows()) == 1
    adapter_calls.assert_not_called()


@pytest.mark.asyncio
async def test_only_private_goal_kind_can_cross_noninternal_automation_boundary(parked):
    from gateway.session_synthetic import mark_synthetic
    _runner, adapter, authority, source, sid, key, _manager, rows, _ = parked
    for kind in ('loop', 'recovery', 'notification', 'heartbeat'):
        event = MessageEvent(text='other synthetic kind', source=source, internal=False,
                             metadata={kind: True, 'gateway_session_key': key, 'gateway_session_id': sid})
        mark_synthetic(event)
        with pytest.raises(RuntimeStoreError) as refused:
            await authority.admit_automation(adapter, event, 'not-a-goal:' + kind)
        assert (refused.value.reason, refused.value.field) == ('invalid_params', 'automation_event')
    assert rows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['new_wait', 'definition', 'edit', 'pause', 'reset', 'insert_failure'])
async def test_goal_ack_and_admission_are_one_fenced_transaction(parked, monkeypatch, change):
    from gateway.session_route_wake import admit_route_wake_receipt
    from gateway.run_goal_wait import goal_wait_guard, goal_wait_identity

    runner, adapter, authority, source, sid, key, manager, rows, _ = parked
    event = captured_event(runner, source, sid, manager)
    identity = goal_wait_identity(authority, sid, manager.state)
    def edit():
        manager.state.goal = 'same instance but revised definition'
        manager._save()

    def refuse_insert():
        authority.db._execute_write(lambda conn: conn.execute('''CREATE TRIGGER refuse_goal_admission
            BEFORE INSERT ON session_admissions BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END'''))

    {'new_wait': lambda: manager.wait_for_seconds(60, 'newer wait'),
     'definition': lambda: manager.set('replacement goal'),
     'edit': edit, 'pause': lambda: manager.pause('user pause'),
     'reset': lambda: runner.session_store.reset_session(key),
     'insert_failure': refuse_insert}[change]()
    before = goals.load_goal(sid).to_json()
    import sqlite3
    try:
        receipt = await admit_route_wake_receipt(runner, adapter, source, event.text, producer='goal-wait',
            metadata=event.metadata, event=event, identity=identity,
            _authorize_write=goal_wait_guard(authority, event))
    except (RuntimeStoreError, sqlite3.IntegrityError):
        receipt = None
    assert receipt is None
    assert rows() == []
    assert goals.load_goal(sid).to_json() == before


@pytest.mark.asyncio
async def test_cancelled_observer_does_not_split_commit_from_goal_ack(parked, monkeypatch):
    runner, _adapter, authority, _source, sid, _key, _manager, rows, _ = parked
    entered, release = threading.Event(), threading.Event()
    original = authority.db._execute_write

    def blocked(write, *args, **kwargs):
        entered.set()
        assert release.wait(20), 'test did not release the writer'
        return original(write, *args, **kwargs)

    monkeypatch.setattr(authority.db, '_execute_write', blocked)
    task = asyncio.create_task(runner._goal_wakeup_fire_one(sid))
    try:
        await async_wait_until(entered.is_set, timeout=20, what='shielded commit')
        # The loop is responsive even while the write is blocked.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    await async_wait_until(lambda: len(rows()) == 1, timeout=20, what='cancelled observer commit')
    assert goals.load_goal(sid).waiting_since == 0
    assert runner._get_replay_scheduler().gate.load() == 1
    await runner._goal_wakeup_fire_one(sid)
    assert len(rows()) == 1


@pytest.mark.asyncio
async def test_fast_drain_reparks_without_old_ack_erasing_new_wait(parked, monkeypatch):
    runner, _adapter, authority, _source, sid, _key, _manager, rows, adapter_calls = parked
    monkeypatch.setattr(authority, '_schedule', type(authority)._schedule.__get__(authority))
    reparked = []

    async def turn(event, *args, **kwargs):
        fresh = goals.GoalManager(sid)
        fresh.wait_for_seconds(900, reason='new wait armed by the admitted turn')
        reparked.append(copy.deepcopy(goals.load_goal(sid)))
        result = execution_result.get()
        assert result is not None
        result['result'] = {'final_response': 'parked again', 'messages': []}
        return 'parked again'

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    await runner._goal_wakeup_fire_one(sid)
    await async_wait_until(lambda: bool(reparked) and rows()[0]['status'] == 'terminal',
                           timeout=20, what='goal repark settlement')
    current = goals.load_goal(sid)
    assert current.waiting_since == reparked[0].waiting_since
    assert current.waiting_until == reparked[0].waiting_until
    assert current.waiting_until > time.time()
    adapter_calls.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_watchers_and_new_wait_generation_admit_once_each(parked, monkeypatch):
    from gateway.run_goals import post_turn_bookkeeping_pending

    runner, _adapter, authority, _source, sid, key, _manager, rows, adapter_calls = parked
    await asyncio.gather(*(runner._goal_wakeup_fire_one(sid) for _ in range(3)))
    assert len(rows()) == 1
    old_identity = rows()[0]['request_id']
    # Let the admitted row finish before arming a new wait; queued work correctly
    # suppresses a second watcher dispatch for that route.
    monkeypatch.setattr(authority, '_schedule', type(authority)._schedule.__get__(authority))
    from gateway.session_contract import SessionRef
    authority._schedule(SessionRef(authority.profile_id, sid))
    await async_wait_until(lambda: rows()[0]['status'] == 'terminal'
                           and not runner._is_session_running(key)
                           and not post_turn_bookkeeping_pending(runner, key)
                           and (authority.sessions[sid].task is None or authority.sessions[sid].task.done()),
                           timeout=20, what='first wait generation fully released')
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    fresh = goals.GoalManager(sid)
    fresh.wait_for_seconds(1, reason='second wait generation')
    monkeypatch.setattr(goals.time, 'time', lambda: fresh.state.waiting_until
                        + goals.DEFAULT_MIN_CONTINUATION_GAP_SECONDS + 10)
    monkeypatch.setattr('gateway.run_goal_wait.monotonic', lambda: 1e12)
    await asyncio.gather(*(runner._goal_wakeup_fire_one(sid) for _ in range(3)))
    assert len(rows()) == 2
    assert rows()[1]['request_id'] != old_identity
    assert goals.load_goal(sid).waiting_since == 0
    adapter_calls.assert_not_called()


@pytest.mark.asyncio
async def test_background_full_keeps_wait_without_admission(parked, monkeypatch):
    runner, _adapter, _authority, _source, sid, _key, _manager, rows, adapter_calls = parked
    monkeypatch.setattr(runner, '_reserve_background_turn', lambda *args: None)
    before = goals.load_goal(sid).to_json()
    await runner._goal_wakeup_fire_one(sid)
    assert goals.load_goal(sid).to_json() == before
    assert rows() == []
    adapter_calls.assert_not_called()


@pytest.mark.asyncio
async def test_admitted_goal_lost_head_settles_interrupted_not_human_loss(parked):
    from gateway.session_contract import SessionRef
    from gateway.session_settlement_recovery import recover_failed_settlement
    from gateway.session_unattended_unknown import settle_unattended_unknown
    from hermes_state_runtime import claim_session_input
    from agent.turn_failure_copy import AUTOMATION_INTERRUPTED_NOTICE
    runner, _adapter, authority, _source, sid, _key, _manager, rows, _ = parked
    await runner._goal_wakeup_fire_one(sid)
    claimed = claim_session_input(authority.db, epoch=authority.epoch, session_id=sid)
    assert isinstance(claimed, dict)
    authority.db.append_message(sid, 'user', claimed['payload']['text'])
    await recover_failed_settlement(authority, claimed)
    ref = SessionRef(authority.profile_id, sid)
    assert rows()[0]['status'] == 'unknown'
    assert await settle_unattended_unknown(authority, ref) is True
    assert (rows()[0]['status'], rows()[0]['outcome']) == ('terminal', 'interrupted')
    assert goals.load_goal(sid).waiting_since == 0
    assert await settle_unattended_unknown(authority, ref) is False
    notices = [m['content'] for m in authority.db.get_messages(sid)
               if m.get('display_kind') == 'failed_turn']
    assert notices == [AUTOMATION_INTERRUPTED_NOTICE]


@pytest.mark.asyncio
async def test_single_profile_goal_ack_does_not_invert_routing_save_locks(parked, monkeypatch):
    from gateway.run_goal_wait import goal_wait_guard
    from hermes_state_runtime import admit_session_input

    runner, _, authority, source, sid, key, manager, rows, _ = parked
    store = runner.session_store
    assert store._routing_db is authority.db
    guard = goal_wait_guard(authority, captured_event(runner, source, sid, manager))
    saving, writing = threading.Event(), threading.Event()
    errors = []
    lookup = store.lookup_by_session_key

    def bounded_lookup(route):
        # Bound the broken lock inversion so RED rolls back and fixture teardown
        # can close the actual DB rather than leaving deadlocked daemon threads.
        if not store._lock.acquire(timeout=3):
            raise TimeoutError('goal acknowledgement blocked on SessionStore lock')
        try:
            return lookup(route)
        finally:
            store._lock.release()

    monkeypatch.setattr(store, 'lookup_by_session_key', bounded_lookup)

    def save():
        try:
            with store._lock:
                saving.set()
                assert writing.wait(5)
                store._save()
        except Exception as exc:
            errors.append(exc)

    def authorize(conn):
        writing.set()
        guard(conn)

    def admit():
        try:
            assert saving.wait(5)
            admit_session_input(authority.db, epoch=authority.epoch,
                principal_id='automation:' + key, session_id=sid,
                request_id='lock-order-wake', payload={'text': 'lock-order wake'},
                _authorize_write=authorize)
        except Exception as exc:
            errors.append(exc)

    saver = threading.Thread(target=save, daemon=True)
    writer = threading.Thread(target=admit, daemon=True)
    saver.start()
    writer.start()
    await asyncio.to_thread(writer.join, 5)
    await asyncio.to_thread(saver.join, 5)
    assert not writer.is_alive() and not saver.is_alive(), 'routing/admission deadlock'
    assert errors == [], f'routing/admission lock inversion: {errors}'
    assert len(rows()) == 1
    assert goals.load_goal(sid).waiting_since == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['reset', 'suspend', 'remove'])
async def test_goal_ack_rejects_changed_durable_route(parked, change):
    import json
    from gateway.run_goal_wait import goal_wait_guard
    from hermes_state_runtime import admit_session_input

    runner, _, authority, source, sid, key, manager, rows, _ = parked
    guard = goal_wait_guard(authority, captured_event(runner, source, sid, manager))
    before = goals.load_goal(sid).to_json()
    scope = runner.session_store._routing_scope()

    def rotate(conn):
        row = conn.execute('SELECT entry_json FROM gateway_routing WHERE scope=? AND session_key=?',
                           (scope, key)).fetchone()
        assert row is not None
        if change == 'remove':
            conn.execute('DELETE FROM gateway_routing WHERE scope=? AND session_key=?', (scope, key))
        else:
            route = json.loads(row[0])
            route['session_id' if change == 'reset' else 'suspended'] = 'replacement' if change == 'reset' else True
            conn.execute('UPDATE gateway_routing SET entry_json=? WHERE scope=? AND session_key=?',
                         (json.dumps(route), scope, key))

    authority.db._execute_write(rotate)
    with pytest.raises(RuntimeStoreError, match='admission_conflict'):
        admit_session_input(authority.db, epoch=authority.epoch,
            principal_id='automation:' + key, session_id=sid, request_id='stale-route-wake',
            payload={'text': 'stale route wake'}, _authorize_write=guard)
    assert rows() == []
    assert goals.load_goal(sid).to_json() == before


@pytest.mark.asyncio
async def test_local_route_wake_reads_live_sessions_on_owner_loop():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from gateway.config import Platform
    from gateway.session_route_wake import admit_route_wake_receipt
    owner = threading.get_ident()

    class LoopOwnedSessions(dict):
        def get(self, key, default=None):
            assert threading.get_ident() == owner, 'worker read loop-owned sessions'
            return super().get(key, default)

    key, sid = 'agent:main:local:fixture', 'local-fixture'
    source = SimpleNamespace(platform=Platform.LOCAL, chat_id=sid)
    receipt = SimpleNamespace(status='queued')
    authority = SimpleNamespace(sessions=LoopOwnedSessions({sid: SimpleNamespace(source=source, route=key)}),
                                admit_automation=AsyncMock(return_value=receipt))
    entry = SimpleNamespace(session_id=sid, suspended=False)
    runner = SimpleNamespace(session_authority=authority,
        _session_key_for_source=Mock(return_value=key),
        session_store=SimpleNamespace(lookup_by_session_key=Mock(return_value=entry)))
    authority.runner = runner
    assert await admit_route_wake_receipt(runner, object(), source, 'wake', 'local-tick',
                                          identity='owner-loop') is receipt
    admitted = authority.admit_automation.call_args.args[1]
    assert admitted.source is source


@pytest.mark.asyncio
async def test_receipt_lookup_failure_still_backs_off_and_logs_refusal(parked, monkeypatch, caplog):
    runner, _, authority, _, sid, _, _, rows, _ = parked
    before = goals.load_goal(sid).to_json()

    async def refuse(*args, **kwargs):
        raise RuntimeStoreError('queue_full')

    def unavailable(*args, **kwargs):
        raise OSError('receipt lookup unavailable')

    monkeypatch.setattr(authority, 'admit_automation', refuse)
    monkeypatch.setattr('hermes_state_runtime.find_session_admission', unavailable)
    with caplog.at_level('INFO', logger='gateway.run_goal_wait'):
        await runner._goal_wakeup_fire_one(sid)
    assert sid in getattr(runner, '_goal_wait_retries', {}), 'lookup failure skipped admission backoff'
    assert 'queue_full' in caplog.text and 'retrying admission' in caplog.text
    assert goals.load_goal(sid).to_json() == before
    assert rows() == []
    assert runner._get_replay_scheduler().gate.load() == 0


@pytest.mark.asyncio
async def test_receipt_loss_after_commit_cannot_admit_twice(parked, monkeypatch):
    runner, _adapter, authority, _source, sid, _key, _manager, rows, _ = parked
    original = authority.admit_automation

    async def lose_receipt(*args, **kwargs):
        await original(*args, **kwargs)
        raise ConnectionError('receipt lost after durable commit')

    with monkeypatch.context() as loss:
        loss.setattr(authority, 'admit_automation', lose_receipt)
        await runner._goal_wakeup_fire_one(sid)
    assert goals.load_goal(sid).waiting_since == 0
    assert len(rows()) == 1
    await runner._goal_wakeup_fire_one(sid)
    assert len(rows()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('rotation', ['compression', 'reset', 'fork'])
async def test_not_found_reresolve_is_compression_only(parked, monkeypatch, rotation):
    runner, _adapter, authority, _source, sid, key, _manager, rows, _ = parked
    original = authority.admit_automation
    attempts = []
    child = sid + '-child'

    async def rotate_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            if rotation == 'compression':
                authority.db.publish_compression_child(parent_session_id=sid, child_session_id=child,
                    source='discord', messages=[{'role': 'user', 'content': 'summary'}],
                    require_compression_lease=False)
                goals.migrate_goal_to_session(sid, child)
                runner.session_store.advance_compression_session(key, sid, child)
            else:
                entry = runner.session_store.reset_session(key)
                if rotation == 'fork':
                    authority.db.create_session(child, source='discord', parent_session_id=sid)
                    entry.session_id = child
            raise RuntimeStoreError('not_found')
        return await original(*args, **kwargs)

    monkeypatch.setattr(authority, 'admit_automation', rotate_once)
    await runner._goal_wakeup_fire_one(sid)
    if rotation == 'compression':
        assert len(rows()) == 1
        assert goals.load_goal(child).waiting_since == 0
        assert len(attempts) == 2
    else:
        assert rows() == []
        assert goals.load_goal(sid).waiting_since > 0
        assert goals.load_goal(sid).last_automatic_continuation_at == 0
        assert len(attempts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('session_change', ['none', 'ended', 'archived', 'new_head'])
async def test_secondary_profile_wait_admits_without_taking_launch_db_lock(tmp_path, monkeypatch, session_change):
    from gateway.run import _profile_runtime_scope
    from gateway.run_goal_wait import goal_wait_guard
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session import SessionSource
    from hermes_state_runtime import admit_session_input
    from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner

    root, homes = _reserve_homes(tmp_path, monkeypatch, names=('alpha',))
    process_ownership.reserve([home for _, home in homes])
    runner = _runner(root, homes)
    try:
        await initialize_gateway_runtime(runner)
        with _profile_runtime_scope(homes[1][1], {}):
            authority = runner.session_authorities.for_home(homes[1][1])
            source = SessionSource(platform=Platform.DISCORD, chat_id='42', user_id='42',
                                   chat_type='dm', profile='alpha')
            entry = runner.session_store.get_or_create_session(source)
            key, sid = entry.session_key, entry.session_id
            authority.db.create_session(sid, source='discord', session_key=key)
            runner.session_store._save_entry(key)
            routing_db = runner.session_store._routing_db
            assert routing_db is not authority.db
            scope = runner.session_store._routing_scope()
            with routing_db._read_ctx() as conn:
                assert conn.execute('SELECT 1 FROM gateway_routing WHERE scope=? AND session_key=?',
                                    (scope, key)).fetchone()
            await runner._warm_goals_session_db('multiplex wait fixture')
            manager = goals.GoalManager(sid)
            manager.set('finish the secondary profile goal')
            manager.wait_for_seconds(1, 'disposable evidence')
            guard = await asyncio.to_thread(goal_wait_guard, authority,
                                           captured_event(runner, source, sid, manager))
            before = goals.load_goal(sid).to_json()
            if session_change == 'ended':
                authority.db._execute_write(lambda conn: conn.execute(
                    'UPDATE sessions SET ended_at=1 WHERE id=?', (sid,)))
            elif session_change == 'archived':
                authority.db._execute_write(lambda conn: conn.execute(
                    'UPDATE sessions SET archived=1 WHERE id=?', (sid,)))
            elif session_change == 'new_head':
                authority.db._execute_write(lambda conn: conn.execute(
                    "UPDATE sessions SET ended_at=1,end_reason='compression' WHERE id=?", (sid,)))
                authority.db.create_session('new-head', source='discord', parent_session_id=sid)
            # A routing writer may itself wait on this authority. Never borrow its
            # lock from the authority writer, even though both handles are SQLite.
            with monkeypatch.context() as locked:
                def forbidden_read(*args, **kwargs):
                    raise AssertionError('authority writer acquired the launch routing DB')
                locked.setattr(routing_db, '_read_ctx', forbidden_read)
                async def admit():
                    return await asyncio.to_thread(admit_session_input, authority.db, epoch=authority.epoch,
                        principal_id='automation:' + key, session_id=sid, request_id='multiplex-goal-wait',
                        payload={'text': 'secondary goal wake'}, _authorize_write=guard)
                if session_change == 'none':
                    await admit()
                else:
                    with pytest.raises(RuntimeStoreError, match='admission_conflict'):
                        await admit()
            if session_change == 'none':
                assert len(list_session_admissions(authority.db, session_id=sid)) == 1
                assert goals.load_goal(sid).waiting_since == 0
                assert goals.load_goal(sid).last_automatic_continuation_at > 0
            else:
                assert list_session_admissions(authority.db, session_id=sid) == []
                assert goals.load_goal(sid).to_json() == before
    finally:
        for authority in list(getattr(runner, 'session_authorities', []) or []):
            authority.db.close()
        runner.session_store.close_all_db_handles()
        for _, home in homes:
            process_ownership.release(home)


@pytest.mark.asyncio
async def test_private_topic_suspends_wait_without_refusal_and_resumes_after_off(parked, caplog):
    # A private interval suspends the goal (#1033): no terminal refusal, no retry, no FIFO row.
    runner, _adapter, _authority, _source, sid, key, _manager, rows, adapter_calls = parked
    store = runner.session_store
    before = goals.load_goal(sid).to_json()
    store.set_incognito(key, True)
    try:
        with caplog.at_level('WARNING', logger='gateway.run_goal_wait'):
            for _ in range(3):
                await runner._goal_wakeup_fire_one(sid)
    finally:
        store.set_incognito(key, False)
    assert not [r for r in caplog.records if r.name == 'gateway.run_goal_wait' and r.levelname == 'WARNING']
    assert sid not in getattr(runner, '_goal_wait_retries', {})
    assert sid not in getattr(runner, '_goal_wait_terminal', {})
    assert rows() == []
    assert goals.load_goal(sid).to_json() == before
    assert runner._get_replay_scheduler().gate.load() == 0
    adapter_calls.assert_not_called()
    await runner._goal_wakeup_fire_one(sid)
    assert len(rows()) == 1, 'the suspended wait resumes once the topic is public again'


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['incognito', 'missing_durable_route'])
async def test_unfenceable_wait_is_terminal_once_without_retry_tracebacks(parked, monkeypatch, caplog, route):
    runner, _adapter, authority, _source, sid, key, _manager, rows, adapter_calls = parked
    store = runner.session_store
    authority.db._execute_write(lambda conn: conn.execute(
        'DELETE FROM gateway_routing WHERE scope=? AND session_key=?', (store._routing_scope(), key)))
    before = goals.load_goal(sid).to_json()
    from itertools import count
    clock = count(1e12, 1000)
    monkeypatch.setattr('gateway.run_goal_wait.monotonic', lambda: next(clock))
    try:
        if route == 'incognito':
            store.set_incognito(key, True)
            # The wakeup normally suspends a private topic first; this covers a switch that
            # raced past that check, which the durable route snapshot still refuses.
            monkeypatch.setattr('gateway.run_goals._goal_suspended_while_private', lambda _entry: False)
        with caplog.at_level('WARNING', logger='gateway.run_goal_wait'):
            for _ in range(3):
                await runner._goal_wakeup_fire_one(sid)
    finally:
        if route == 'incognito':
            store.set_incognito(key, False)
    assert sid not in getattr(runner, '_goal_wait_retries', {}), 'permanent refusal kept retrying'
    warnings = [r for r in caplog.records if r.name == 'gateway.run_goal_wait' and r.levelname == 'WARNING']
    assert len(warnings) == 1
    assert route in warnings[0].message and sid in warnings[0].message
    assert warnings[0].exc_info is None
    assert rows() == []
    assert goals.load_goal(sid).to_json() == before
    assert runner._get_replay_scheduler().gate.load() == 0
    adapter_calls.assert_not_called()


@pytest.mark.asyncio
async def test_secondary_profile_watcher_uses_native_goal_only_admission(tmp_path, monkeypatch):
    from gateway.run import _profile_runtime_scope
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_synthetic import is_synthetic
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner

    root, homes = _reserve_homes(tmp_path, monkeypatch, names=('alpha',))
    process_ownership.reserve([home for _, home in homes])
    runner = _runner(root, homes)
    (homes[1][1] / '.env').write_text('DISCORD_ALLOWED_USERS=42\n')
    try:
        await initialize_gateway_runtime(runner)
        with _profile_runtime_scope(homes[1][1], {}):
            monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
            adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture', typing_indicator=False))
            adapter.gateway_runner = runner
            runner._profile_adapters = {'alpha': {Platform.DISCORD: adapter}}
            runner._configure_profile_adapter(adapter, 'alpha', Platform.DISCORD)
            source = adapter.build_source(chat_id='42', user_id='42', chat_type='dm')
            monkeypatch.setattr(adapter, 'send', AsyncMock(return_value=SendResult(success=True)))
            monkeypatch.setattr(runner, '_handle_message_with_agent', AsyncMock(return_value='seed reply'))
            await adapter.handle_message(MessageEvent(text='seed', source=source, message_id='seed'))
            await async_wait_until(lambda: not adapter._background_tasks, timeout=20, what='secondary seed')
            key = runner._session_key_for_source(source)
            sid = runner.session_store.peek_session_id(key)
            assert sid is not None
            authority = runner.session_authorities.for_home(homes[1][1])
            seed = list_session_admissions(authority.db, session_id=sid, pending_only=False)
            assert len(seed) == 1 and seed[0]['status'] == 'terminal'
            await runner._warm_goals_session_db('secondary goal wait')
            manager = goals.GoalManager(sid)
            manager.set('finish the secondary profile goal')
            manager.wait_for_seconds(1, 'disposable evidence')
            manager.state.waiting_until = time.time() - 1
            manager._save()
            monkeypatch.setattr(authority, '_schedule', lambda *args: None)
            adapter_calls = AsyncMock(wraps=adapter.handle_message)
            monkeypatch.setattr(adapter, 'handle_message', adapter_calls)
            await runner._goal_wakeup_fire_one(sid)
            rows = list_session_admissions(authority.db, session_id=sid)
            assert len(rows) == 1
            descriptor = rows[0]['payload']['native_text_v1']['synthetic']
            assert descriptor['metadata']['goal_continuation'] is True and descriptor['internal'] is False
            assert goals.load_goal(sid).waiting_since == 0
            assert sid not in getattr(runner, '_goal_wait_retries', {})
            from gateway.session_envelope import restore_native, check_native_route
            event = restore_native(rows[0]['payload'], runner)
            _bound_source, bound_key = await check_native_route(runner, rows[0]['payload'], sid, source, adapter)
            assert bound_key == key and is_synthetic(event)
            adapter_calls.assert_not_called()
    finally:
        for authority in list(getattr(runner, 'session_authorities', []) or []):
            authority.db.close()
        runner.session_store.close_all_db_handles()
        for _, home in homes:
            process_ownership.release(home)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['clear', 'reset'])
async def test_retry_and_terminal_maps_pruned_when_goal_no_longer_parked(parked, monkeypatch, change):
    runner, _adapter, _authority, _source, sid, key, manager, _rows, _calls = parked
    runner._goal_wait_retries = {sid: ('stale-wait', 2, 1e12)}
    runner._goal_wait_terminal = {sid: ('stale-wait', 'missing_durable_route')}
    if change == 'clear':
        manager.clear()
    else:
        runner.session_store.reset_session(key)
    runner._running = True
    sleeps = 0
    async def one_scan(_interval):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:  # first sleep is the startup connection grace period
            runner._running = False
    monkeypatch.setattr('gateway.run_goals.asyncio.sleep', one_scan)
    await runner._loop_wakeup_watcher(interval=0)
    assert sid not in runner._goal_wait_retries
    assert sid not in runner._goal_wait_terminal
