"""An invalid runtime must not consume a durable completion before it can run."""
from types import SimpleNamespace

import pytest

from gateway.session_contract import SessionRef
from gateway.session_automation_retry import retry_state
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_automation_notification_category import _native_owner


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['completion', 'async_delegation'])
@pytest.mark.parametrize('refusal', ['runtime', 'invalid_config', 'claimed'])
async def test_refuse_runtime_once_then_recover_one_durable_delivery(tmp_path, monkeypatch, kind, refusal):
    from gateway import session_automation_retry as retry
    from gateway.session_results import execution_result
    runner, authority, _adapter, source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
    ref = SessionRef(authority.profile_id, metadata['gateway_session_id'])
    seen = []

    async def execute(event):
        if event.internal:
            if clock.now == 1000.0:
                if refusal == 'claimed':
                    result = {'pre_execution_runtime_refusal': True,
                        'failed': True, 'messages': [], 'tools': [], 'api_calls': 0}
                else:
                    # Exercise the actual refusal marker at model resolution, not an
                    # admission-side probe. Successful turns below need no real provider.
                    from gateway.run_turn_runner import TurnRunner
                    turn = SimpleNamespace(_runner=runner, _combined_ephemeral_prompt=lambda: '',
                        _ctx=SimpleNamespace(source=source, session_key=metadata['gateway_session_key'],
                                             user_config={}))
                    result = TurnRunner._run_sync_scoped(turn)
                assert result['pre_execution_runtime_refusal'] is True
                assert retry.held_runtime_refusal(event, result)
                return
            runtime()
        seen.append(event.text)
        execution_result.get()['result'] = {'final_response': '', 'messages': [], 'api_calls': 1}

    monkeypatch.setattr(runner, '_handle_message', execute)
    try:
        await authority._drain(ref)  # The fixture's human seed.
        seen.clear()
        evt = dict(type=kind, event_id='result', session_id='proc', started_at=1.0,
                   delegation_id='deleg-result', session_key=metadata['gateway_session_key'],
                   parent_session_id=metadata['gateway_session_id'])
        assert await runner._inject_watch_notification('completed work', evt) is True
        row = list_session_admissions(authority.db, session_id=ref.session_id)[0]
        from gateway.session_envelope import restore_native
        expected_text = restore_native(row['payload'], runner).text
        clock = SimpleNamespace(now=1000.0)
        monkeypatch.setattr(retry, 'time', SimpleNamespace(time=lambda: clock.now))
        monkeypatch.setattr('gateway.run._gateway_config_home', lambda: tmp_path)
        attempts = []

        if refusal == 'invalid_config':
            import json
            (tmp_path / 'config.yaml').write_text(json.dumps({
                'model_presets': {'fast': {'provider': 'fixture', 'model': 'fixture'}},
                'delegation': {'model_preset': 'fast', 'provider': 'conflict'}}))

        def runtime(**kwargs):
            attempts.append(clock.now)
            if refusal == 'invalid_config':
                from gateway.run import _load_gateway_runtime_config
                _load_gateway_runtime_config()
            if refusal == 'runtime' and len(attempts) == 1:
                raise ValueError('model_preset cannot be combined with inline route fields')
            return 'fixture', {}

        monkeypatch.setattr(runner, '_resolve_session_agent_runtime', runtime)
        monkeypatch.setattr('gateway.session_policy.policy_for_source', lambda *_args: None)
        monkeypatch.setattr('gateway.run._current_max_iterations', lambda: 1)
        first, _ = await authority._claim_next(ref, authority.sessions[ref.session_id])
        assert first['status'] == 'started'
        assert attempts == []  # Claiming never resolves a model or reads provider credentials.
        assert await authority._execute_claimed(ref, authority.sessions[ref.session_id], first) == (None, 'deferred')
        pending = list_session_admissions(authority.db, session_id=ref.session_id)
        assert len(pending) == 1 and pending[0]['status'] == 'queued'
        assert retry_state(authority.db, row['admission_id']) == {'attempt': 1, 'due_at': 1010.0}
        assert seen == []

        # Recreate the authority and epoch from the store, as startup does. A queued input is
        # not an unknown/started turn and must survive recovery with the same retry deadline.
        from hermes_state import SessionDB
        from tests.fakes.runtime_owner import initialize_session_authority
        from gateway.session_route_recovery import restore_routed_session
        authority.db.close()
        db = SessionDB(tmp_path / 'state.db')
        authority = await initialize_session_authority(runner, profile_id=ref.profile_id,
            instance_id='recovered-owner', db=db)
        monkeypatch.setattr(authority, '_schedule', lambda *_args: None)
        assert restore_routed_session(authority, ref.session_id) is not None
        assert retry_state(db, row['admission_id'])['due_at'] == 1010.0
        assert list_session_admissions(db, session_id=ref.session_id)[0]['status'] == 'queued'
        clock.now = 1009.0
        assert (await authority._claim_next(ref, authority.sessions[ref.session_id]))[0] == 'automation_wait'
        assert attempts == ([1000.0] if refusal != 'claimed' else [])
        if refusal == 'invalid_config':
            (tmp_path / 'config.yaml').write_text('{}')
        clock.now = 1010.0
        await authority._drain(ref)
        assert seen == [expected_text]
        assert attempts == ([1000.0, 1010.0] if refusal != 'claimed' else [1010.0])
        assert list_session_admissions(authority.db, session_id=ref.session_id) == []
        assert retry_state(authority.db, row['admission_id']) == {}
        # The same producer result remains an ACK, not another turn, after recovery.
        assert await runner._inject_watch_notification('completed work', dict(evt)) is True
        await authority._drain(ref)
        assert seen == [expected_text]
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_only_proven_refusals_delay_the_fifo_with_bounded_backoff(tmp_path, monkeypatch):
    from gateway import session_automation_retry as retry
    from gateway.session_results import execution_result
    runner, authority, _adapter, _source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
    ref = SessionRef(authority.profile_id, metadata['gateway_session_id'])
    clock = SimpleNamespace(now=1000.0)
    seen = []
    unavailable = True

    async def execute(event):
        if event.internal and unavailable:
            execution_result.get()['result'] = {'pre_execution_runtime_refusal': True,
                'failed': True, 'messages': [], 'tools': [], 'api_calls': 0}
        else:
            seen.append(event.text)
            execution_result.get()['result'] = {'final_response': '', 'messages': [], 'api_calls': 1}

    def no_admission_probe(**_kwargs):
        pytest.fail('admission must not resolve a model before choosing the executor')

    monkeypatch.setattr(runner, '_handle_message', execute)
    monkeypatch.setattr(runner, '_resolve_session_agent_runtime', no_admission_probe)
    monkeypatch.setattr(retry, 'time', SimpleNamespace(time=lambda: clock.now))
    try:
        await authority._drain(ref)
        seen.clear()
        def inject(name):
            return runner._inject_watch_notification(name, dict(type='completion',
                event_id=name, session_id=name, started_at=1.0,
                session_key=metadata['gateway_session_key'], parent_session_id=ref.session_id))

        assert await inject('blocked result') is True
        identity = list_session_admissions(authority.db, session_id=ref.session_id)[0]['admission_id']
        pending = None
        for attempt, delay in enumerate((10, 20, 40, 60, 60), 1):
            row, _ = await authority._claim_next(ref, authority.sessions[ref.session_id])
            assert row['admission_id'] == identity and row['status'] == 'started'
            assert await authority._execute_claimed(ref, authority.sessions[ref.session_id], row) == (None, 'deferred')
            assert retry_state(authority.db, identity) == {
                'attempt': min(attempt, 4), 'due_at': clock.now + delay}
            if pending is None:
                # Completions queued together fold into one batch turn, so the follower lands
                # after the refusal: it must queue behind the held head, not fold into it and
                # replace it with an admission that has no backoff.
                assert await inject('queued follower') is True
                pending = list_session_admissions(authority.db, session_id=ref.session_id)
                assert len(pending) == 2 and pending[0]['admission_id'] == identity
            queued = list_session_admissions(authority.db, session_id=ref.session_id)
            assert [item['admission_id'] for item in queued] == [item['admission_id'] for item in pending]
            assert all(item['status'] == 'queued' for item in queued)
            assert queued[0]['owner_epoch'] is None and queued[0]['generation'] is None
            assert seen == []
            clock.now += delay - 1
            assert (await authority._claim_next(ref, authority.sessions[ref.session_id]))[0] == 'automation_wait'
            clock.now += 1
        unavailable = False
        await authority._drain(ref)
        assert len(seen) == 2 and 'blocked result' in seen[0] and 'queued follower' in seen[1]
        assert list_session_admissions(authority.db, session_id=ref.session_id) == []
        assert retry_state(authority.db, identity) == {}
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_persistently_refused_completion_yields_its_head_to_human_input(tmp_path, monkeypatch):
    """A refusal that never clears (a broken model route) keeps retrying the completion, but a
    person's message queued behind it runs instead of waiting behind the backoff forever."""
    from gateway import session_automation_retry as retry
    from gateway.platforms.event import MessageEvent
    from gateway.session_ingress_context import native_callback
    from gateway.session_results import execution_result
    from tests.gateway.test_automation_notification_category import _SOURCE
    runner, authority, adapter, _source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
    ref = SessionRef(authority.profile_id, metadata['gateway_session_id'])
    clock = SimpleNamespace(now=1000.0)
    seen = []

    async def execute(event):
        if event.internal:
            execution_result.get()['result'] = {'pre_execution_runtime_refusal': True,
                'failed': True, 'messages': [], 'tools': [], 'api_calls': 0}
        else:
            seen.append(event.text)
            execution_result.get()['result'] = {'final_response': '', 'messages': [], 'api_calls': 1}

    async def human(text, message_id):
        event = MessageEvent(text=text, source=adapter.build_source(**_SOURCE, message_id=message_id),
                             message_id=message_id)
        with native_callback(runner, event, tmp_path):
            await authority.admit_native(event)

    async def claim_and_run():
        live = authority.sessions[ref.session_id]
        row, _ = await authority._claim_next(ref, live)
        assert isinstance(row, dict) and row['status'] == 'started'
        return row, await authority._execute_claimed(ref, live, row)

    monkeypatch.setattr(runner, '_handle_message', execute)
    monkeypatch.setattr(retry, 'time', SimpleNamespace(time=lambda: clock.now))
    try:
        await authority._drain(ref)
        seen.clear()
        assert await runner._inject_watch_notification('blocked result', dict(type='completion',
            event_id='blocked', session_id='blocked', started_at=1.0,
            session_key=metadata['gateway_session_key'], parent_session_id=ref.session_id)) is True
        identity = list_session_admissions(authority.db, session_id=ref.session_id)[0]['admission_id']
        for attempt, message_id in enumerate(('h1', 'h2', 'h3'), 1):
            row, outcome = await claim_and_run()
            assert row['admission_id'] == identity and outcome == (None, 'deferred')
            assert retry_state(authority.db, identity)['attempt'] == attempt
            await human(f'human {attempt}', message_id)
            # Still inside the backoff: the person's turn runs now, the completion stays queued.
            row, (response, outcome) = await claim_and_run()
            assert row['admission_id'] != identity
            from gateway.session_settlement_recovery import settle_and_deliver
            await settle_and_deliver(authority, authority.sessions[ref.session_id], ref, row, response, outcome)
            assert seen[-1] == f'human {attempt}'
            pending = list_session_admissions(authority.db, session_id=ref.session_id)
            assert [item['admission_id'] for item in pending] == [identity]
            assert pending[0]['status'] == 'queued'
            assert (await authority._claim_next(ref, authority.sessions[ref.session_id]))[0] == 'automation_wait'
            clock.now = retry_state(authority.db, identity)['due_at']
        assert seen == ['human 1', 'human 2', 'human 3']
    finally:
        authority.db.close()


def test_real_turn_runner_stamps_resolution_refusal_before_agent_construction(monkeypatch):
    from gateway.config import Platform
    from gateway.run_turn_runner import TurnRunner
    from gateway.session_results import execution_result
    from gateway.session_automation_retry import held_runtime_refusal

    def refuse(**_kwargs):
        raise ValueError('model_preset cannot be combined with inline route fields')

    monkeypatch.setattr('gateway.session_policy.policy_for_source', lambda *_args: None)
    monkeypatch.setattr('gateway.run._current_max_iterations', lambda: 1)
    runner = SimpleNamespace(_resolve_session_agent_runtime=refuse)
    turn = SimpleNamespace(_runner=runner, _combined_ephemeral_prompt=lambda: '',
        _ctx=SimpleNamespace(source=SimpleNamespace(platform=Platform.TELEGRAM),
            session_key='parent', user_config={}))
    result = TurnRunner._run_sync_scoped(turn)
    assert result['pre_execution_runtime_refusal'] is True
    assert result['api_calls'] == 0 and result['messages'] == [] and result['tools'] == []
    captured = {}
    token = execution_result.set(captured)
    try:
        assert held_runtime_refusal(SimpleNamespace(internal=True), result)
        assert captured['terminal_result'] is result
        assert not held_runtime_refusal(SimpleNamespace(internal=False), result)
    finally:
        execution_result.reset(token)


@pytest.mark.asyncio
async def test_a_stop_accepted_before_a_refusal_settles_the_claim_interrupted(tmp_path, monkeypatch):
    """A refusal must not requeue work a Stop already ended: it would run once the model or
    credentials recover, after the person stopped it."""
    from gateway import session_automation_retry as retry
    from gateway.session_contract import SessionActor
    from gateway.session_results import execution_result
    from hermes_state_runtime import get_session_admission
    runner, authority, _adapter, _source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
    ref = SessionRef(authority.profile_id, metadata['gateway_session_id'])
    actor = SessionActor('app-viewer', authority.profile_id, frozenset({'session:control'}), 'viewer')
    clock = SimpleNamespace(now=1000.0)
    seen, stop = [], SimpleNamespace(on=False, handle=None)

    async def execute(event):
        if event.internal:
            if stop.on:
                stop.handle = await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
            execution_result.get()['result'] = {'pre_execution_runtime_refusal': True,
                'failed': True, 'messages': [], 'tools': [], 'api_calls': 0}
        else:
            seen.append(event.text)
            execution_result.get()['result'] = {'final_response': '', 'messages': [], 'api_calls': 1}

    monkeypatch.setattr(runner, '_handle_message', execute)
    monkeypatch.setattr(retry, 'time', SimpleNamespace(time=lambda: clock.now))
    try:
        await authority._drain(ref)
        seen.clear()
        assert await runner._inject_watch_notification('stopped result', dict(type='completion',
            event_id='stopped', session_id='stopped', started_at=1.0,
            session_key=metadata['gateway_session_key'], parent_session_id=ref.session_id)) is True
        identity = list_session_admissions(authority.db, session_id=ref.session_id)[0]['admission_id']
        live = authority.sessions[ref.session_id]
        row, _ = await authority._claim_next(ref, live)
        assert await authority._execute_claimed(ref, live, row) == (None, 'deferred')
        assert retry_state(authority.db, identity)['attempt'] == 1

        clock.now = retry_state(authority.db, identity)['due_at']
        stop.on = True
        row, _ = await authority._claim_next(ref, live)
        assert row['admission_id'] == identity
        response, outcome = await authority._execute_claimed(ref, live, row)
        assert stop.handle is not None and stop.handle.execution_state == 'running'  # Accepted.
        assert outcome != 'deferred'
        from gateway.session_settlement_recovery import settle_and_deliver
        await settle_and_deliver(authority, live, ref, row, response, outcome)
        settled = get_session_admission(authority.db, admission_id=identity)
        assert (settled['status'], settled['outcome']) == ('terminal', 'interrupted')
        assert list_session_admissions(authority.db, session_id=ref.session_id) == []
        assert retry_state(authority.db, identity) == {}  # No orphaned retry clock.
        assert seen == []
    finally:
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('committed', [False, True], ids=['lost_write', 'committed_then_raised'])
async def test_a_storage_failure_recording_a_refusal_retains_the_unexecuted_input(tmp_path, monkeypatch, committed):
    """The refusal proved nothing ran: a transient write failure must not settle it ``failed``.
    A write that committed before raising is recognized, not counted as a second attempt."""
    import sqlite3
    from gateway import session_automation_retry as retry
    from gateway.session_results import execution_result
    runner, authority, _adapter, _source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
    ref = SessionRef(authority.profile_id, metadata['gateway_session_id'])
    clock = SimpleNamespace(now=1000.0)
    seen, failures = [], []

    async def execute(event):
        if event.internal:
            execution_result.get()['result'] = {'pre_execution_runtime_refusal': True,
                'failed': True, 'messages': [], 'tools': [], 'api_calls': 0}
        else:
            seen.append(event.text)
            execution_result.get()['result'] = {'final_response': '', 'messages': [], 'api_calls': 1}

    record = retry._record_refusal

    def flaky(*args, **kwargs):
        if not failures:
            failures.append(True)
            if committed:
                record(*args, **kwargs)
            raise sqlite3.OperationalError('disk I/O error')
        return record(*args, **kwargs)

    monkeypatch.setattr(runner, '_handle_message', execute)
    monkeypatch.setattr(retry, 'time', SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr(retry, '_record_refusal', flaky)
    try:
        await authority._drain(ref)
        seen.clear()
        assert await runner._inject_watch_notification('kept result', dict(type='completion',
            event_id='kept', session_id='kept', started_at=1.0,
            session_key=metadata['gateway_session_key'], parent_session_id=ref.session_id)) is True
        identity = list_session_admissions(authority.db, session_id=ref.session_id)[0]['admission_id']
        live = authority.sessions[ref.session_id]
        row, _ = await authority._claim_next(ref, live)
        assert await authority._execute_claimed(ref, live, row) == (None, 'deferred')
        assert failures == [True]
        pending = list_session_admissions(authority.db, session_id=ref.session_id)
        assert [(item['admission_id'], item['status']) for item in pending] == [(identity, 'queued')]
        assert retry_state(authority.db, identity) == {'attempt': 1, 'due_at': 1010.0}
        assert seen == []
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_a_failed_retry_clock_cleanup_never_fails_the_executed_turn(tmp_path, monkeypatch):
    """The retried completion ran and answered: losing the write that clears its old refusal clock
    must not settle it ``failed`` without its response, nor leave a clock that retries it. The next
    automation settle sweeps the leftover clock."""
    import sqlite3
    from gateway import session_automation_retry as retry
    from gateway.session_results import execution_result
    from gateway.session_settlement_recovery import settle_and_deliver
    from hermes_state_runtime import get_session_admission
    runner, authority, _adapter, _source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
    ref = SessionRef(authority.profile_id, metadata['gateway_session_id'])
    clock = SimpleNamespace(now=1000.0)
    seen, failures, sent = [], [], []
    unavailable = True

    async def execute(event):
        if event.internal and unavailable:
            execution_result.get()['result'] = {'pre_execution_runtime_refusal': True,
                'failed': True, 'messages': [], 'tools': [], 'api_calls': 0}
            return None
        seen.append(event.text)
        execution_result.get()['result'] = {'final_response': f'answer {len(seen)}', 'messages': [], 'api_calls': 1}
        return f'answer {len(seen)}'

    clear = retry._clear_refusal

    def flaky(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise sqlite3.OperationalError('database is locked')
        return clear(*args, **kwargs)

    async def send(chat_id, content, *args, **kwargs):
        sent.append(content)
        return SimpleNamespace(success=True, message_id=f'm{len(sent)}', error=None)

    monkeypatch.setattr(runner, '_handle_message', execute)
    monkeypatch.setattr(retry, 'time', SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr(retry, '_clear_refusal', flaky)
    monkeypatch.setattr(_adapter, 'send', send)

    def inject(name):
        return runner._inject_watch_notification(name, dict(type='completion', event_id=name,
            session_id=name, started_at=1.0, session_key=metadata['gateway_session_key'],
            parent_session_id=ref.session_id))

    try:
        await authority._drain(ref)
        seen.clear()
        sent.clear()
        live = authority.sessions[ref.session_id]
        assert await inject('retried result') is True
        identity = list_session_admissions(authority.db, session_id=ref.session_id)[0]['admission_id']
        row, _ = await authority._claim_next(ref, live)
        assert await authority._execute_claimed(ref, live, row) == (None, 'deferred')
        assert retry_state(authority.db, identity)['attempt'] == 1

        clock.now = retry_state(authority.db, identity)['due_at']
        unavailable = False
        row, _ = await authority._claim_next(ref, live)
        assert row['admission_id'] == identity
        response, outcome = await authority._execute_claimed(ref, live, row)
        assert failures == [True]
        assert (response, outcome) == ('answer 1', 'completed')
        await settle_and_deliver(authority, live, ref, row, response, outcome)
        settled = get_session_admission(authority.db, admission_id=identity)
        assert (settled['status'], settled['outcome']) == ('terminal', 'completed')
        assert sent == ['answer 1']
        assert retry_state(authority.db, identity)  # The lost cleanup left its clock behind.
        # The settled admission is never claimed again, whatever its leftover clock says.
        assert list_session_admissions(authority.db, session_id=ref.session_id) == []
        await authority._drain(ref)
        assert seen == [seen[0]] and 'retried result' in seen[0]

        # The next automation turn's settle sweeps the orphaned clock.
        assert await inject('later result') is True
        await authority._drain(ref)
        assert len(seen) == 2 and 'later result' in seen[1]
        assert retry_state(authority.db, identity) == {}
    finally:
        authority.db.close()
