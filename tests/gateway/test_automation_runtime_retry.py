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
    runner, authority, adapter, source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
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
                return None
            runtime()
        seen.append(event.text)
        execution_result.get()['result'] = {'final_response': '', 'messages': [], 'api_calls': 1}
        return None

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
        from gateway.session_authority import initialize_session_authority
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
    runner, authority, adapter, source, metadata = await _native_owner(tmp_path, monkeypatch, 'seed')
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
        for identity in ('blocked result', 'queued follower'):
            assert await runner._inject_watch_notification(identity, dict(type='completion',
                event_id=identity, session_id=identity, started_at=1.0,
                session_key=metadata['gateway_session_key'], parent_session_id=ref.session_id)) is True
        pending = list_session_admissions(authority.db, session_id=ref.session_id)
        identity = pending[0]['admission_id']
        for attempt, delay in enumerate((10, 20, 40, 60, 60), 1):
            row, _ = await authority._claim_next(ref, authority.sessions[ref.session_id])
            assert row['admission_id'] == identity and row['status'] == 'started'
            assert await authority._execute_claimed(ref, authority.sessions[ref.session_id], row) == (None, 'deferred')
            assert retry_state(authority.db, identity) == {
                'attempt': min(attempt, 4), 'due_at': clock.now + delay}
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
