"""Detached gateway handshake timeouts defer an unstarted cron occurrence."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json

import pytest

from cron import executions, jobs, scheduler, scheduler_authority
from hermes_cli import gateway_client
from agent.runtime_session_store import GatewayHandshakeTimeout

def test_attach_retries_connect_timeout_then_adopts(monkeypatch):
    from cron import detached_authority

    sleeps = []
    monkeypatch.setattr(detached_authority.time, "sleep", sleeps.append)
    calls = []

    class RPC:
        def __call__(self, method, **_params):
            calls.append(method)
            if len(calls) <= 3:
                raise GatewayHandshakeTimeout('connect')
            return {"owner_epoch": 7} if method == "worker.adopt" else None

    result = detached_authority._attach_worker(
        RPC(), {"execution_id": "e", "session_id": "s", "generation": 1}
    )

    assert result == {"owner_epoch": 7}
    assert calls == ["worker.register"] * 3 + ["worker.register", "worker.adopt"]
    assert sleeps == [0.1, 0.2, 0.4]


def test_exhausted_attach_timeout_is_phase_labeled():
    from cron import detached_authority

    class RPC:
        def __call__(self, _method, **_params):
            raise GatewayHandshakeTimeout('connect')

    with pytest.raises(detached_authority.DetachedHandshakeTimeout, match=r"\(connect\)"):
        detached_authority._attach_worker(
            RPC(), {"execution_id": "e", "session_id": "s", "generation": 1}
        )


def test_exhausted_owner_transition_is_phase_labeled():
    from cron import detached_authority
    from hermes_state_errors import GatewayOwnerRestartError

    class RPC:
        def __call__(self, _method, **_params):
            raise GatewayOwnerRestartError('owner_restart: gateway did not return')

    with pytest.raises(detached_authority.DetachedHandshakeTimeout, match=r"\(owner-transition\)"):
        detached_authority._attach_worker(
            RPC(), {"execution_id": "e", "session_id": "s", "generation": 1}
        )


def test_run_one_job_defers_marked_handshake_before_delivery(occurrence_home, monkeypatch):
    from cron.scheduler_authority import NOT_STARTED_FLAG

    jobs, executions, scheduler, counter = occurrence_home
    claimed = _due_occurrence(occurrence_home, 'recurring')

    def run_job(job, **_kwargs):
        job[NOT_STARTED_FLAG] = True
        return False, "", "", "gateway handshake timed out (connect)"

    monkeypatch.setattr(scheduler, "run_job", run_job)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_a, **_kw: pytest.fail("must defer"))
    assert scheduler._run_one_job_body(claimed) is True
    assert executions.get_execution(claimed['execution_id'])['delivery_outcome'] == 'deferred'
    assert jobs.get_job(claimed['id'])['pending_slot']['scheduled_at'] == claimed['_scheduled_instant']
    assert not counter.exists()


@pytest.fixture
def occurrence_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / 'scripts').mkdir(parents=True)
    counter = home / 'fires.txt'
    script = home / 'scripts' / 'fire.sh'
    script.write_text(f'#!/bin/sh\necho fired >> "{counter}"\necho fired\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(scheduler, '_hermes_home', home)
    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', home / 'cron' / 'executions.db')
    scheduler._running_job_ids.clear()
    scheduler._running_since.clear()
    scheduler._running_futures.clear()
    with jobs.use_cron_store(home / 'cron'):
        yield jobs, executions, scheduler, counter
    scheduler._shutdown_parallel_pool()


def _due_occurrence(env, kind):
    jobs, executions, _scheduler, _counter = env
    slot = (jobs._hermes_now() - timedelta(seconds=30)).replace(microsecond=0).isoformat()
    job = jobs.create_job(prompt=None, schedule='every 1h' if kind == 'recurring' else 'in 1m',
                          script='fire.sh', no_agent=True, deliver='local')
    jobs.update_job(job['id'], {'next_run_at': slot})
    claimed = jobs.claim_job_for_fire(job['id'], return_job=True)
    assert claimed and claimed['_scheduled_instant']
    execution = executions.create_execution(job['id'], source='tick',
                                            scheduled_instant=claimed['_scheduled_instant'])
    return dict(claimed, execution_id=execution['id'])


@pytest.mark.parametrize('kind', ['recurring', 'oneshot'])
def test_handshake_deferral_reclaims_same_occurrence_on_next_tick(occurrence_home, monkeypatch, kind):
    jobs, executions, scheduler, counter = occurrence_home
    claimed = _due_occurrence(occurrence_home, kind)

    def timeout(job, **_kwargs):
        job['_gateway_not_started'] = True
        return False, '', '', 'gateway handshake timed out (connect)'

    with monkeypatch.context() as patch:
        patch.setattr(scheduler, 'run_job', timeout)
        assert scheduler._run_one_job_body(claimed)
    current = jobs.get_job(claimed['id'])
    assert not current.get('failure_streak') and current['last_run_at'] is None
    if kind == 'oneshot':
        assert current['repeat']['completed'] == 0, 'refund the one-shot preclaim'
    else:
        assert current['pending_slot']['scheduled_at'] == claimed['_scheduled_instant']
    assert not counter.exists()

    if kind == 'oneshot':
        # #1281 behavior: the one-shot retry instant is refreshed to now+60s.
        retry_at = datetime.fromisoformat(current['next_run_at'])
        monkeypatch.setattr(jobs, '_hermes_now', lambda: retry_at + timedelta(seconds=1))
    scheduler.tick(verbose=False, sync=True)
    assert counter.read_text().splitlines() == ['fired']
    row = executions.latest_execution(claimed['id'])
    assert row['id'] != claimed['execution_id'] and row['status'] == 'completed'
    assert row['scheduled_instant'] == (current['next_run_at'] if kind == 'oneshot' else claimed['_scheduled_instant'])
    scheduler.tick(verbose=False, sync=True)
    assert counter.read_text().splitlines() == ['fired']



@pytest.mark.parametrize('lateness', [30, 400])
def test_oneshot_prebody_loss_reuses_1281_refresh(occurrence_home, monkeypatch, lateness):
    """One-shot lateness after a long outage is pre-existing #1281 behavior: now+60 refresh."""
    jobs, _executions, scheduler, counter = occurrence_home
    claimed = _due_occurrence(occurrence_home, 'oneshot')
    now = datetime.fromisoformat(claimed['_scheduled_instant']) + timedelta(seconds=lateness)
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now)

    def timeout(job, **_kwargs):
        job['_gateway_not_started'] = True
        return False, '', '', 'gateway handshake timed out (connect)'

    with monkeypatch.context() as patch:
        patch.setattr(scheduler, 'run_job', timeout)
        assert scheduler._run_one_job_body(claimed)
    current = jobs.get_job(claimed['id'])
    assert 'pending_slot' not in current and not current.get('failure_streak')
    assert current['repeat']['completed'] == 0
    assert datetime.fromisoformat(current['next_run_at']) == now + timedelta(seconds=60)
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now + timedelta(seconds=61))
    scheduler.tick(verbose=False, sync=True)
    assert counter.read_text().splitlines() == ['fired']
    scheduler.tick(verbose=False, sync=True)
    assert counter.read_text().splitlines() == ['fired']


def test_plain_late_oneshot_without_deferral_still_retires(occurrence_home, monkeypatch):
    jobs, _executions, scheduler, counter = occurrence_home
    job = jobs.create_job(prompt=None, schedule='in 1m', script='fire.sh', no_agent=True, deliver='local')
    slot = job['next_run_at']
    monkeypatch.setattr(jobs, '_hermes_now', lambda: datetime.fromisoformat(slot) + timedelta(seconds=400))
    scheduler.tick(verbose=False, sync=True)
    current = jobs.get_job(job['id'])
    assert current['state'] == 'missed' and not current['enabled']
    assert not counter.exists()




def test_refused_deferral_releases_claim_and_records_failure(occurrence_home, monkeypatch):
    from cron import scheduler_control

    jobs, executions, scheduler, _counter = occurrence_home
    claimed = _due_occurrence(occurrence_home, 'recurring')

    def timeout(job, **_kwargs):
        job['_gateway_not_started'] = True
        return False, '', '', 'gateway handshake timed out (connect)'

    monkeypatch.setattr(scheduler, 'run_job', timeout)
    monkeypatch.setattr(scheduler_control, 'defer_unstarted_fire', lambda *_a, **_k: False)
    scheduler._run_one_job_body(claimed)
    current = jobs.get_job(claimed['id'])
    assert current.get('fire_claim') is None
    assert current['last_status'] == 'error'
    assert current['failure_streak'] == 1
    assert executions.get_execution(claimed['execution_id'])['delivery_outcome'] != 'deferred'

def test_restored_recurring_slot_obeys_expiry_bounds(occurrence_home, monkeypatch):
    from cron.scheduler_control import defer_unstarted_fire

    jobs, _executions, scheduler, counter = occurrence_home
    claimed = _due_occurrence(occurrence_home, 'recurring')
    jobs.update_job(claimed['id'], {'max_catch_up_lateness_seconds': 0})
    scheduled = datetime.fromisoformat(claimed['_scheduled_instant'])
    assert defer_unstarted_fire(claimed, 'pre-body handshake timeout')
    monkeypatch.setattr(jobs, '_hermes_now', lambda: scheduled + timedelta(days=2))
    scheduler.tick(verbose=False, sync=True)
    current = jobs.get_job(claimed['id'])
    assert not counter.exists()
    assert not current.get('pending_slot')
    assert datetime.fromisoformat(current['next_run_at']) > scheduled + timedelta(days=1)


@pytest.mark.parametrize('error', [
    'admission_conflict', 'producer_not_started', 'permission_denied',
    'Detached cron run exceeded hard wall-clock timeout (30s).',
])
def test_deterministic_prebody_refusals_use_visible_failure_lane(occurrence_home, error):
    from cron import scheduler_worker_failure

    jobs, executions, _scheduler, _counter = occurrence_home
    claimed = _due_occurrence(occurrence_home, 'recurring')
    fire = claimed['execution_id']
    assert executions.mark_execution_handoff_pending(fire)
    assert executions.adopt_claimed_execution(fire)
    assert executions.mark_detached_attaching(fire)
    assert executions.finish_execution(fire, success=False, error=error)
    assert scheduler_worker_failure.record_external_worker_outcome(
        claimed, execution_id=fire, worker_exited=True)
    current = jobs.get_job(claimed['id'])
    assert current['last_status'] == 'error'
    assert current['failure_streak'] == 1
    assert executions.get_execution(fire)['delivery_outcome'] != 'deferred'


@pytest.mark.parametrize('kind', ['recurring', 'oneshot'])
def test_reaped_prebody_worker_requeues_without_counting_failure(occurrence_home, monkeypatch, kind):
    from cron import scheduler_worker_failure

    jobs, executions, scheduler, counter = occurrence_home
    claimed = _due_occurrence(occurrence_home, kind)
    assert jobs.claim_dispatch(claimed['id'])
    fire = claimed['execution_id']
    assert executions.mark_execution_handoff_pending(fire)
    assert executions.adopt_claimed_execution(fire)
    assert executions.mark_detached_attaching(fire)
    assert executions.mark_detached_attach_lost(fire)
    with executions._transaction() as conn:
        conn.execute("UPDATE executions SET status='unknown' WHERE id=?", (fire,))
    assert scheduler_worker_failure.record_external_worker_outcome(claimed, worker_exited=True)
    current = jobs.get_job(claimed['id'])
    assert not current.get('failure_streak') and current['last_run_at'] is None
    if kind == 'oneshot':
        monkeypatch.setattr(jobs, '_hermes_now', lambda: datetime.fromisoformat(current['next_run_at']) + timedelta(seconds=1))
    scheduler.tick(verbose=False, sync=True)
    assert counter.read_text().splitlines() == ['fired']
    assert executions.latest_execution(claimed['id'])['scheduled_instant'] == (
        current['next_run_at'] if kind == 'oneshot' else claimed['_scheduled_instant'])
    # An old waiter is idempotent and cannot refund/restore this slot twice.
    assert scheduler_worker_failure.record_external_worker_outcome(claimed, worker_exited=True)
    scheduler.tick(verbose=False, sync=True)
    assert counter.read_text().splitlines() == ['fired']


@pytest.mark.parametrize('edit', [
    {'enabled': False, 'state': 'paused'},
    {'schedule': {'kind': 'interval', 'minutes': 120}},
    {'script': 'replacement.sh'},
])
@pytest.mark.parametrize('kind', ['recurring', 'oneshot'])
def test_operator_edit_after_deferral_does_not_revive_occurrence(occurrence_home, edit, kind):
    jobs, _executions, scheduler, counter = occurrence_home
    from cron.scheduler_control import defer_unstarted_fire

    claimed = _due_occurrence(occurrence_home, kind)
    assert defer_unstarted_fire(claimed, 'prebody handshake timeout')
    jobs.update_job(claimed['id'], edit)
    scheduler.tick(verbose=False, sync=True)
    assert not counter.exists()
    assert not jobs.get_job(claimed['id']).get('pending_slot')


@pytest.mark.parametrize('phase', [None, 'started'])
def test_unknown_started_or_legacy_worker_is_never_requeued(occurrence_home, monkeypatch, phase):
    from cron import scheduler_worker_failure

    jobs, executions, scheduler, counter = occurrence_home
    claimed = _due_occurrence(occurrence_home, 'recurring')
    fire = claimed['execution_id']
    executions.mark_execution_handoff_pending(fire)
    executions.adopt_claimed_execution(fire)
    if phase:
        assert executions.mark_detached_attaching(fire)
        assert executions.start_detached_body(fire)
    with executions._transaction() as conn:
        conn.execute("UPDATE executions SET status='unknown' WHERE id=?", (fire,))
    monkeypatch.setattr(scheduler, '_deliver_crash_failure', lambda *_a, **_k: (None, None))
    assert scheduler_worker_failure.record_external_worker_outcome(claimed, worker_exited=True)
    assert executions.get_execution(fire)['status'] == 'unknown'
    assert not jobs.get_job(claimed['id']).get('pending_slot')
    scheduler.tick(verbose=False, sync=True)
    assert not counter.exists()


def test_lost_register_answer_retries_same_fenced_scope_without_body_rerun(monkeypatch):
    from cron import detached_authority

    seen = []
    scope = {'execution_id': 'original-worker', 'session_id': 's', 'generation': 8,
             'admission_id': 'same-admission'}
    monkeypatch.setattr(detached_authority.time, 'sleep', lambda _delay: None)

    def rpc(method, **params):
        seen.append((method, params))
        if len(seen) == 1:
            # The owner may have accepted this registration before its answer vanished.
            raise TimeoutError('worker.register phase=answer')
        return {'owner_epoch': 4} if method == 'worker.adopt' else None

    assert detached_authority._attach_worker(rpc, scope) == {'owner_epoch': 4}
    assert [method for method, _params in seen] == ['worker.register', 'worker.register', 'worker.adopt']
    assert all({key: value for key, value in params.items() if key != 'kind'} == scope
               for _method, params in seen)


def test_body_gate_cannot_be_won_after_unknown_terminalization(occurrence_home):
    _jobs, executions, _scheduler, _counter = occurrence_home
    claimed = _due_occurrence(occurrence_home, 'recurring')
    fire = claimed['execution_id']
    executions.mark_execution_handoff_pending(fire)
    executions.adopt_claimed_execution(fire)
    assert executions.mark_detached_attaching(fire)
    with executions._transaction() as conn:
        conn.execute("UPDATE executions SET status='unknown' WHERE id=?", (fire,))
    assert not executions.start_detached_body(fire)
    assert executions.get_execution(fire)['body_phase'] == 'attaching'


@pytest.mark.parametrize('method', ['register', 'adopt'])
def test_exhausted_attach_answer_timeout_is_prebody_deferral(monkeypatch, method):
    from cron import detached_authority

    calls = []
    scope = {'execution_id': 'original', 'session_id': 's', 'generation': 1}
    monkeypatch.setattr(detached_authority.time, 'sleep', lambda _delay: None)

    def rpc(name, **params):
        calls.append((name, params))
        if name == 'worker.' + method:
            raise TimeoutError('worker_receipt_timeout')

    with pytest.raises(detached_authority.DetachedHandshakeTimeout, match=method + '-answer'):
        detached_authority._attach_worker(rpc, scope)
    assert sum(name == 'worker.' + method for name, _ in calls) == 4
    assert all(params['execution_id'] == 'original' for _, params in calls)


def _prebody_journal(env, monkeypatch, kind, pause_kind):
    from utils import atomic_json_write

    jobs, executions, _scheduler, _counter = env
    claimed = _due_occurrence(env, kind)
    fire = claimed['execution_id']
    assert jobs.claim_dispatch(claimed['id'])
    assert executions.mark_execution_handoff_pending(fire)
    assert executions.adopt_claimed_execution(fire)
    assert executions.mark_detached_attaching(fire)
    assert executions.mark_detached_attach_lost(fire)
    with executions._transaction() as conn:
        conn.execute("UPDATE executions SET status='unknown' WHERE id=?", (fire,))
    journal = scheduler_authority.journal_path(claimed['id'], fire)
    atomic_json_write(journal, {
        'params': {'job_id': claimed['id'], 'request_id': fire, 'extra_prompt': None},
        'job': claimed, 'receipt': {'admission_id': 'old', 'session_id': 's'},
    }, mode=0o600, fsync_dir=True)
    if pause_kind in {'recovery', 'operator'}:
        scheduler_authority.pause_for_unknown(claimed['id'], fire, 'Canonical cron unverified')
    if pause_kind == 'operator':
        jobs.pause_job(claimed['id'], reason='Operator hold')
    return claimed, journal


@pytest.mark.parametrize('kind', ['recurring', 'oneshot'])
@pytest.mark.parametrize('pause_kind', ['none', 'recovery', 'operator'])
def test_prebody_journal_recovery_obeys_exact_pause_owner(occurrence_home, monkeypatch, kind, pause_kind):
    from cron import scheduler_control

    jobs, executions, scheduler, counter = occurrence_home
    calls = []
    original_defer = scheduler_control.defer_unstarted_fire

    def counted_defer(*args, **kwargs):
        calls.append(1)
        return original_defer(*args, **kwargs)

    monkeypatch.setattr(scheduler_control, 'defer_unstarted_fire', counted_defer)
    claimed, journal = _prebody_journal(occurrence_home, monkeypatch, kind, pause_kind)
    scheduler_authority.reconcile_pending(allow_connect=False)
    current = jobs.get_job(claimed['id'])
    assert calls == [1]
    if pause_kind == 'operator':
        assert journal.exists()
        assert current['paused_reason'] == 'Operator hold'
        assert current['last_run_at'] is None and not current.get('failure_streak')
        assert not counter.exists()
        return
    assert not journal.exists()
    assert current.get('paused_reason') is None
    assert current['last_run_at'] is None and not current.get('failure_streak')
    if kind == 'oneshot' and pause_kind != 'operator':
        monkeypatch.setattr(jobs, '_hermes_now', lambda: datetime.fromisoformat(current['next_run_at']) + timedelta(seconds=1))
    scheduler.tick(verbose=False, sync=True)
    if pause_kind == 'operator':
        assert jobs.get_job(claimed['id'])['paused_reason'] == 'Operator hold'
        assert not counter.exists()
    else:
        assert counter.read_text().splitlines() == ['fired']
        assert executions.latest_execution(claimed['id'])['scheduled_instant'] == (
            current['next_run_at'] if kind == 'oneshot' else claimed['_scheduled_instant'])


@pytest.mark.parametrize('kind', ['recurring', 'oneshot'])
def test_recovery_fence_refusal_stays_on_base_path(occurrence_home, monkeypatch, kind):
    from cron import scheduler_control

    jobs, _executions, _scheduler, _counter = occurrence_home
    claimed, journal = _prebody_journal(occurrence_home, monkeypatch, kind, 'recovery')
    with monkeypatch.context() as patch:
        patch.setattr(scheduler_control, 'defer_unstarted_fire', lambda *_a, **_k: False)
        scheduler_authority.reconcile_pending(allow_connect=False)
    assert journal.exists()
    current = jobs.get_job(claimed['id'])
    assert current['state'] == 'paused'
    assert current['paused_reason'] == 'Canonical cron unverified'
    assert not current.get('failure_streak')


@pytest.mark.parametrize('kind', ['recurring', 'oneshot'])
def test_real_worker_death_before_body_reclaims_exact_occurrence(occurrence_home, monkeypatch, kind):
    import os
    import subprocess
    import sys
    from cron import scheduler_worker_failure

    _jobs, executions, scheduler, counter = occurrence_home
    claimed = _due_occurrence(occurrence_home, kind)
    assert _jobs.claim_dispatch(claimed['id'])
    fire = claimed['execution_id']
    assert executions.mark_execution_handoff_pending(fire)
    source = (
        'import os, sys; from cron import executions as e; '
        'assert e.adopt_claimed_execution(sys.argv[1]); '
        'assert e.mark_detached_attaching(sys.argv[1]); os._exit(137)'
    )
    proc = subprocess.run([sys.executable, '-c', source, fire], env=dict(os.environ),
                          capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 137, proc.stderr
    executions.recover_interrupted_executions()
    assert executions.get_execution(fire)['status'] == 'unknown'
    assert scheduler_worker_failure.record_external_worker_outcome(claimed, worker_exited=True)
    retry_at = _jobs.get_job(claimed['id']).get('next_run_at')
    if kind == 'oneshot':
        monkeypatch.setattr(_jobs, '_hermes_now', lambda: datetime.fromisoformat(
            _jobs.get_job(claimed['id'])['next_run_at']) + timedelta(seconds=1))
    scheduler.tick(verbose=False, sync=True)
    assert counter.read_text().splitlines() == ['fired']
    assert executions.latest_execution(claimed['id'])['scheduled_instant'] == (
        retry_at if kind == 'oneshot' else claimed['_scheduled_instant'])


@pytest.mark.parametrize('prebody', [True, False])
def test_canonical_detached_timeout_uses_shared_flag_only_before_body(occurrence_home, monkeypatch, prebody):
    from cron import detached_authority, scheduler_transport
    from gateway import session_cron

    _jobs, _executions, _scheduler, _counter = occurrence_home
    claimed = _due_occurrence(occurrence_home, 'recurring')

    @asynccontextmanager
    async def connected(_params):
        async def call(operation, _data):
            if operation == 'submit':
                return {'session_id': 's', 'admission_id': 'same-admission'}
            return {'status': 'started', 'job': claimed}
        yield call

    def timeout(*_args, **_kwargs):
        error_type = detached_authority.DetachedHandshakeTimeout if prebody else TimeoutError
        raise error_type('worker_receipt_timeout')

    monkeypatch.setattr(session_cron, 'owner_for_home', lambda _home: None)
    monkeypatch.setattr(scheduler_transport, 'recovering_cron_client', connected)
    monkeypatch.setattr(detached_authority, 'owns_detached_execution', lambda *_a: True)
    monkeypatch.setattr(detached_authority, 'run_detached', timeout)
    if prebody:
        result = scheduler_authority.run_canonical_job(claimed)
        assert not result[0] and result[3] == 'worker_receipt_timeout'
        assert claimed[scheduler_authority.NOT_STARTED_FLAG] is True
    else:
        with pytest.raises(scheduler_authority.CronExecutionUnknown):
            scheduler_authority.run_canonical_job(claimed)
        assert scheduler_authority.NOT_STARTED_FLAG not in claimed
