"""Pre-inference persistence refusals keep their verdict across owner receipts and restart."""
import asyncio
import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from cron import executions, jobs, scheduler, scheduler_authority
from gateway import session_cron, session_results
from gateway.config import Platform
from gateway.session_contract import SessionRef
from hermes_state_runtime import claim_session_input
from tests.fakes import cron_owner as owners


@pytest.fixture
def runtime(monkeypatch):
    import run_agent
    import hermes_state_registry
    import hermes_cli.env_loader
    import hermes_cli.runtime_provider

    # The actual owner and run-side acquisition share the canonical registry handle.
    monkeypatch.setattr(owners, 'SessionDB', hermes_state_registry.acquire)
    monkeypatch.setattr(scheduler, 'load_config', dict)
    monkeypatch.setattr(scheduler, '_get_session_db_timeout', lambda: .2)
    monkeypatch.setattr(scheduler, '_init_cron_mcp_tools', lambda *_: None)
    monkeypatch.setattr(hermes_cli.env_loader, 'load_hermes_dotenv', lambda **_: None)
    monkeypatch.setattr(hermes_cli.env_loader, 'reset_secret_source_cache', lambda *_: None)
    monkeypatch.setattr(hermes_cli.runtime_provider, 'resolve_runtime_provider', lambda **_: {
        'provider': 'openrouter', 'model': 'test', 'api_key': 'test-key'})
    agent = MagicMock()
    agent.return_value.run_conversation.return_value = {'final_response': 'report'}
    monkeypatch.setattr(run_agent, 'AIAgent', agent)
    return agent


async def execute_and_book(tmp_path, monkeypatch, *, mode, recovered, schedule):
    """Actual admission, owner run, terminal SQLite result, status polling and bookkeeping."""
    import hermes_state_registry

    home = tmp_path / 'profile'
    owner = owners.cron_owner(home, monkeypatch)
    job = jobs.create_job(prompt='Generate a report', schedule=schedule, deliver='local', repeat=2)
    job = jobs.claim_job_for_fire(job['id'], manual=True, return_job=True)
    if job['schedule']['kind'] == 'once':
        assert jobs.claim_dispatch(job['id'])
    fire = executions.create_execution(job['id'], source='test')['id']
    executions.mark_execution_running(fire)
    params = {'job_id': job['id'], 'request_id': fire, 'extra_prompt': None}
    try:
        receipt = await session_cron.operation(owner, 'submit', params)
        sid = receipt['session_id']
        row = claim_session_input(owner.db, epoch=owner.epoch, session_id=sid)
        policy = owner.runner.adapters[Platform.LOCAL].policies[sid]
        with monkeypatch.context() as fault:
            if mode == 'store-unavailable':
                acquire = MagicMock(side_effect=sqlite3.OperationalError('fixture store locked'))
                fault.setattr(hermes_state_registry, 'acquire', acquire)
                gate = MagicMock(side_effect=AssertionError('pre-run gate must not consume state'))
                fault.setattr(scheduler, '_prepare_job_prompt', gate)
            elif mode == 'failed':
                fault.setattr(scheduler, '_run_agent_with_watchdog', MagicMock(
                    side_effect=ValueError('post-admission inference failure')))
            try:
                await session_cron.execute(owner, SessionRef(owner.profile_id, sid), row, policy)
            except RuntimeError:
                assert mode != 'success'
            if mode == 'store-unavailable':
                assert acquire.call_count == 3
                gate.assert_not_called()
        captured = owner.pending_results.pop(row['admission_id'])
        session_results.finish_result(owner.db, epoch=owner.epoch, row=row, response='',
                                      outcome='completed', result=captured)
        assert session_results.admission_result(owner.db, row['admission_id']) == captured
        # Real status transport must put the run-side flags onto the firing scheduler's copy.
        session_cron.bind_owner(owner)
        result = await asyncio.to_thread(scheduler_authority.run_canonical_job, job, execution_id=fire)
        assert result[0] is (mode == 'success')
        journal = scheduler_authority.journal_path(job['id'], fire)
        assert journal.exists()
        if recovered:
            # Lose all process-local result/policy state. Only the original SQLite receipt remains.
            session_cron.unbind_owner(owner)
            previous_db = owner.db
            owner.runner.session_store.close_all_db_handles()
            owner.db.close()
            owner = owners.cron_owner(home, monkeypatch)
            assert owner.db is not previous_db
            assert not owner.pending_results and not owner.sessions
            with executions._transaction() as conn:
                conn.execute('UPDATE executions SET process_id=?,pid=? WHERE id=?',
                             ('departed-firer', 999999, fire))
            session_cron.bind_owner(owner)
            saved_journal = journal.read_text()
            await asyncio.to_thread(scheduler_authority.reconcile_pending, allow_connect=False)
            assert not journal.exists()
            # A duplicated durable journal must not charge the attempt twice.
            journal.write_text(saved_journal)
            await asyncio.to_thread(scheduler_authority.reconcile_pending, allow_connect=False)
            assert not journal.exists()
        else:
            delivery = scheduler._RunDelivery(job, result[0], result[3])
            assert scheduler._finish_completed_run(delivery, job['fire_claim']['by'], fire)
            # Ordinary completion's idempotency fence also rejects repeat charging.
            assert scheduler._finish_completed_run(delivery, None, fire)
        saved = jobs.get_job(job['id'])
        ledger = executions.get_execution(fire)
        print(json.dumps({'mode': mode, 'recovered': recovered, 'schedule': schedule,
                          'flags': captured['result']['cron_job_flags'],
                          'repeat_completed': saved['repeat']['completed'],
                          'status': saved['last_status'], 'execution_status': ledger['status']}, sort_keys=True))
        return saved, ledger
    finally:
        session_cron.unbind_owner(owner)
        owner.runner.session_store.close_all_db_handles()
        owner.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('recovered', [False, True], ids=['ordinary', 'restarted-recovery'])
@pytest.mark.parametrize('schedule', ['in 30m', 'every 5m'])
async def test_store_refusal_does_not_spend_repeat_budget(tmp_path, monkeypatch, runtime, recovered, schedule):
    saved, ledger = await execute_and_book(tmp_path, monkeypatch, mode='store-unavailable',
                                          recovered=recovered, schedule=schedule)
    runtime.assert_not_called()
    assert 'session store unavailable; run not started' in saved['last_error']
    assert saved['last_status'] == 'error' and ledger['status'] == 'failed'
    assert saved['repeat']['completed'] == 0
    assert saved['enabled'] and saved['state'] == 'scheduled'
    assert not saved['fire_claim'] and not saved.get('run_claim')


@pytest.mark.asyncio
@pytest.mark.parametrize('recovered', [False, True], ids=['ordinary', 'restarted-recovery'])
@pytest.mark.parametrize('mode', ['success', 'failed'])
async def test_postadmission_attempt_counts_once(tmp_path, monkeypatch, runtime, recovered, mode):
    saved, ledger = await execute_and_book(tmp_path, monkeypatch, mode=mode,
                                          recovered=recovered, schedule='every 5m')
    runtime.assert_called_once()
    assert saved['repeat']['completed'] == 1
    assert saved['last_status'] == ('ok' if mode == 'success' else 'error')
    assert ledger['status'] == ('completed' if mode == 'success' else 'failed')
