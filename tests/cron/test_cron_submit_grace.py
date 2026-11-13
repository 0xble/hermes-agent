"""A slow owner neither strands a detached fire nor fails one it never reached.

Live (2026-11-12): a stalled gateway answered ``cron.submit`` after 108-129s. The worker's read-only
recovery gave up after its 15s ladder of "missing" reads, so the job was paused and the worker
exited before the owner admitted the fire. Fires whose handshake timed out before any submit were
booked as ordinary failures.
"""
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from cron import executions, jobs, scheduler, scheduler_authority, scheduler_transport
from hermes_cli import gateway_client

PARAMS = {'job_id': 'job', 'request_id': 'fire', 'extra_prompt': None}


def _slow_owner(monkeypatch, *, admitted_after, stalled_reads=()):
    """An owner whose submit answer is lost and that admits the fire *admitted_after* seconds later;
    the recovery reads numbered in *stalled_reads* time out themselves."""
    clock = {'now': 0.0}
    calls, sleeps = [], []
    lost = TimeoutError('cron.submit phase=answer timeout=30s request_id=fire job_id=job')

    class Peer:
        async def rpc(self, method, **params):
            calls.append(method)
            if method == 'cron.submit':
                clock['now'] += 30  # the answer budget
                raise lost
            if calls.count('cron.recover') in stalled_reads:
                raise TimeoutError('cron.recover phase=answer timeout=30s')
            if clock['now'] < admitted_after:
                return {'status': 'missing'}
            return {'status': 'queued', 'session_id': 'cron-session', 'admission_id': 'adm-1'}

    @asynccontextmanager
    async def connected():
        yield Peer()

    async def sleep(delay):
        sleeps.append(delay)
        clock['now'] += delay

    monkeypatch.setattr(gateway_client, 'connect_gateway', connected)
    monkeypatch.setattr(scheduler_transport.asyncio, 'sleep', sleep)
    monkeypatch.setattr(scheduler_transport, 'monotonic', lambda: clock['now'], raising=False)
    return lost, calls, sleeps


@pytest.mark.asyncio
async def test_submit_admitted_after_the_old_ladder_is_recovered(monkeypatch):
    # The owner stays contended: two of the later reads time out as well.
    _lost, calls, sleeps = _slow_owner(monkeypatch, admitted_after=130, stalled_reads=(6, 9))
    async with scheduler_transport.recovering_cron_client(PARAMS) as call:
        receipt = await call('submit', PARAMS)
    assert receipt == {'session_id': 'cron-session', 'admission_id': 'adm-1'}
    # Far past the old 1+2+4+8s ladder, at a bounded polling interval, with reads only.
    assert sum(sleeps) > 15 and max(sleeps) <= scheduler_transport.RECOVERY_MAX_DELAY
    assert calls[0] == 'cron.submit' and set(calls[1:]) == {'cron.recover'}


@pytest.mark.asyncio
async def test_submit_still_missing_after_the_grace_keeps_the_lost_answer(monkeypatch):
    monkeypatch.setattr(scheduler_authority, 'MISSING_SUBMIT_GRACE_SECONDS', 60)
    lost, calls, sleeps = _slow_owner(monkeypatch, admitted_after=float('inf'))
    async with scheduler_transport.recovering_cron_client(PARAMS) as call:
        with pytest.raises(TimeoutError) as caught:
            await call('submit', PARAMS)
    assert caught.value is lost
    # The polling stops once the grace measured from the submit's start has run out.
    assert 30 + sum(sleeps) - max(sleeps) < 60 <= 30 + sum(sleeps)
    assert calls.count('cron.submit') == 1


def test_missing_past_grace_still_fails_the_fire_as_unverified(tmp_path, monkeypatch):
    """The firer's existing verdict for a never-admitted submit is unchanged."""
    monkeypatch.setattr(scheduler_authority, 'MISSING_SUBMIT_GRACE_SECONDS', 60)
    _slow_owner(monkeypatch, admitted_after=float('inf'))
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        with pytest.raises(scheduler_authority.CronExecutionUnknown, match='TimeoutError'):
            scheduler_authority.run_canonical_job(job, execution_id='fire')


def _claimed_due_fire(job_id):
    """Claim the job's current occurrence the way the ticker does."""
    instant = jobs._hermes_now() - timedelta(minutes=1)
    jobs.update_job(job_id, {'next_run_at': instant.isoformat()})
    claimed = jobs.claim_job_for_fire(job_id, return_job=True)
    assert claimed and claimed['_scheduled_instant']
    fire = executions.create_execution(job_id, source='test',
                                       scheduled_instant=claimed['_scheduled_instant'])['id']
    return dict(claimed, execution_id=fire), fire


def test_handshake_timeout_before_submit_defers_the_occurrence(tmp_path, monkeypatch):
    @asynccontextmanager
    async def stalled():
        raise TimeoutError('timed out')  # the control-socket ticket read
        yield

    monkeypatch.setattr(gateway_client, 'connect_gateway', stalled)
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        claimed, fire = _claimed_due_fire(job['id'])
        assert scheduler._run_one_job_body(claimed) is True

        current = jobs.get_job(job['id'])
        assert current['state'] == 'scheduled' and not current.get('paused_at')
        assert current.get('last_status') != 'error' and not current.get('failure_streak')
        assert current['fire_claim'] is None
        assert current['pending_slot']['scheduled_at'] == claimed['_scheduled_instant']
        assert not scheduler_authority.journal_path(job['id'], fire).exists()
        row = executions.get_execution(fire)
        assert row['status'] != 'unknown' and 'Not started; occurrence deferred' in row['error']
        # The scheduler's ordinary due scan puts the same occurrence back.
        assert [due['id'] for due in jobs.get_due_jobs()] == [job['id']]


def test_handshake_timeout_of_a_manual_fire_still_fails_visibly(tmp_path, monkeypatch):
    """A manual run has no occurrence to restore; deferring it would drop it silently."""
    @asynccontextmanager
    async def stalled():
        raise gateway_client.GatewayClientError('Gateway connection failed; no local fallback') \
            from TimeoutError('timed out')
        yield

    monkeypatch.setattr(gateway_client, 'connect_gateway', stalled)
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        assert jobs.claim_job_for_fire(job['id'], manual=True)
        claimed = jobs.get_job(job['id'])
        fire = executions.create_execution(job['id'], source='test')['id']
        assert scheduler._run_one_job_body(dict(claimed, execution_id=fire)) is True
        current = jobs.get_job(job['id'])
        assert current['last_status'] == 'error' and current['state'] != 'paused'
        assert executions.get_execution(fire)['status'] == 'failed'


@pytest.mark.parametrize('state, deferred', [('draining', True), ('starting', True), ('failed', False)])
def test_unavailable_gateway_defers_only_transient_states(tmp_path, monkeypatch, state, deferred):
    @asynccontextmanager
    async def unavailable():
        raise gateway_client.GatewayUnavailableError(f'Gateway {state}: update_paused', state)
        yield

    monkeypatch.setattr(gateway_client, 'connect_gateway', unavailable)
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        claimed, fire = _claimed_due_fire(job['id'])
        scheduler._run_one_job_body(claimed)
        current = jobs.get_job(job['id'])
        assert (current.get('last_status') != 'error') is deferred
        assert ('Not started; occurrence deferred' in executions.get_execution(fire)['error']) is deferred


def _stalled_handshake(monkeypatch, during=lambda: None):
    """A ticket read that times out before any submit, running *during* while it stalls."""
    @asynccontextmanager
    async def stalled():
        during()
        raise TimeoutError('timed out')
        yield

    monkeypatch.setattr(gateway_client, 'connect_gateway', stalled)


@pytest.mark.parametrize('edit', [
    {'schedule': 'every 24h'},
    {'timezone': 'Asia/Tokyo'},
    {'enabled': False, 'state': 'paused', 'paused_at': '2026-11-12T19:00:00-08:00',
     'paused_reason': 'operator'},
])
def test_operator_edit_during_stalled_handshake_is_not_undone(tmp_path, monkeypatch, edit):
    """Live race (2026-11-12 review): an operator edit while the handshake stalls drops the
    occurrence's pending_slot; deferral must not recreate it and overwrite the edited schedule."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        claimed, fire = _claimed_due_fire(job['id'])
        edited = {}
        _stalled_handshake(monkeypatch, lambda: edited.update(jobs.update_job(job['id'], dict(edit))))
        assert scheduler._run_one_job_body(claimed) is True

        current = jobs.get_job(job['id'])
        assert not current.get('pending_slot')
        assert current['next_run_at'] == edited['next_run_at']
        assert current['schedule'] == edited['schedule'] and current.get('timezone') == edited.get('timezone')
        assert 'Not started; occurrence deferred' not in (executions.get_execution(fire)['error'] or '')
        assert claimed['id'] not in [due['id'] for due in jobs.get_due_jobs()]
        assert jobs.get_job(job['id'])['next_run_at'] == edited['next_run_at']


def _claimed_due_oneshot(job_id):
    """Claim a due one-shot the way the ticker does; the run body takes its dispatch preclaim."""
    return _claimed_due_fire(job_id)


def test_oneshot_unedited_handshake_timeout_still_refunds_its_preclaim(tmp_path, monkeypatch):
    _stalled_handshake(monkeypatch)
    with jobs.use_cron_store(tmp_path / 'cron'):
        run_at = (jobs._hermes_now() + timedelta(hours=1)).isoformat()
        job = jobs.create_job(prompt='report', schedule=run_at, deliver='local')
        claimed, fire = _claimed_due_oneshot(job['id'])
        assert scheduler._run_one_job_body(claimed) is True

        current = jobs.get_job(job['id'])
        assert current['repeat']['completed'] == 0 and current['fire_claim'] is None
        assert current['state'] == 'scheduled' and current.get('last_status') != 'error'
        assert 'Not started; occurrence deferred' in executions.get_execution(fire)['error']


def test_oneshot_rescheduled_during_stalled_handshake_keeps_the_new_run_time(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        run_at = (jobs._hermes_now() + timedelta(hours=1)).isoformat()
        job = jobs.create_job(prompt='report', schedule=run_at, deliver='local')
        claimed, fire = _claimed_due_oneshot(job['id'])
        later = (jobs._hermes_now() + timedelta(days=2)).replace(microsecond=0).isoformat()
        edited = {}
        _stalled_handshake(monkeypatch, lambda: edited.update(jobs.update_job(job['id'], {'schedule': later})))
        assert scheduler._run_one_job_body(claimed) is True

        current = jobs.get_job(job['id'])
        assert current['next_run_at'] == edited['next_run_at']
        assert current['schedule'] == edited['schedule']
        assert 'Not started; occurrence deferred' not in (executions.get_execution(fire)['error'] or '')


def test_overlapping_tick_advance_is_not_an_operator_edit(tmp_path, monkeypatch):
    """Review P1 (884bae32): while a recurring fire is in flight, a later tick's
    advance_next_runs rewrites next_run_at. That is not an operator edit, so the unstarted
    occurrence must still be restored, not dropped as superseded."""
    from cron.scheduler_control import defer_unstarted_fire

    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1m', deliver='local')
        claimed, _fire = _claimed_due_fire(job['id'])
        before = jobs.get_job(job['id'])['next_run_at']
        later = jobs._hermes_now() + timedelta(seconds=61)
        monkeypatch.setattr(jobs, '_hermes_now', lambda: later)
        jobs.get_due_jobs()
        assert jobs.advance_next_runs([job['id']]) == 1
        assert jobs.get_job(job['id'])['next_run_at'] != before

        assert defer_unstarted_fire(claimed, 'pre-body handshake timeout') == 'deferred'
        current = jobs.get_job(job['id'])
        assert current['pending_slot']['scheduled_at'] == claimed['_scheduled_instant']
        assert current['fire_claim'] is None


def test_operator_next_run_edit_marks_only_a_live_claim(tmp_path, monkeypatch):
    """update_job's provenance marker: set on a real next_run_at edit of a claimed job, absent
    for an unchanged write and for a job with no claim."""
    from cron.scheduler_control import defer_unstarted_fire

    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        claimed, _fire = _claimed_due_fire(job['id'])
        stored = jobs.get_job(job['id'])['next_run_at']
        jobs.update_job(job['id'], {'next_run_at': stored})
        assert 'superseded_at' not in jobs.get_job(job['id'])['fire_claim']
        edited = (jobs._hermes_now() + timedelta(hours=5)).replace(microsecond=0).isoformat()
        jobs.update_job(job['id'], {'next_run_at': edited})
        assert jobs.get_job(job['id'])['fire_claim']['superseded_at']

        assert defer_unstarted_fire(claimed, 'pre-body handshake timeout') == 'superseded'
        current = jobs.get_job(job['id'])
        assert current['next_run_at'] == edited and not current.get('pending_slot')

        other = jobs.create_job(prompt='other', schedule='every 1h', deliver='local')
        jobs.update_job(other['id'], {'next_run_at': edited})
        assert not jobs.get_job(other['id']).get('fire_claim')
