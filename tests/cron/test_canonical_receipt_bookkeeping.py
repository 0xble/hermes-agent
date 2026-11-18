"""Canonical cron recovery settles attempts and never re-pauses an explicitly resumed job."""
from contextlib import asynccontextmanager
import json

import pytest

from cron import jobs, executions, incidents, scheduler_authority
from hermes_cli import gateway_client


def peer(monkeypatch, call):
    class Peer:
        async def rpc(self, method, **params):
            return call(method, params)
    @asynccontextmanager
    async def connected():
        yield Peer()
    monkeypatch.setattr(gateway_client, 'connect_gateway', connected)


def test_recovered_receipt_finishes_foreign_execution_and_is_idempotent(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        execution = executions.create_execution(job['id'], source='test')
        executions.mark_execution_running(execution['id'])
        with executions._transaction() as conn:
            conn.execute('UPDATE executions SET process_id=?,pid=? WHERE id=?', ('old-owner', 999999, execution['id']))
        params = {'job_id': job['id'], 'request_id': execution['id'], 'extra_prompt': None}
        journal = scheduler_authority.journal_path(job['id'], execution['id'])
        journal.parent.mkdir(parents=True, exist_ok=True)
        saved = json.dumps({'params': params, 'receipt': None})
        journal.write_text(saved)
        peer(monkeypatch, lambda method, params: {'status': 'terminal', 'job': job,
                                                 'result': [True, 'document', 'answer', None]})
        scheduler_authority.reconcile_pending()
        assert executions.get_execution(execution['id'])['status'] == 'completed'
        assert not journal.exists()
        completed = jobs.get_job(job['id'])['repeat']['completed']
        journal.write_text(saved)
        scheduler_authority.reconcile_pending()
        assert jobs.get_job(job['id'])['repeat']['completed'] == completed
        assert executions.get_execution(execution['id'])['delivery_outcome'] == 'suppressed'




def test_dead_firer_recovery_unknown_row_uses_base_receipt_path(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        execution = executions.create_execution(job['id'], source='test')
        executions.mark_execution_running(execution['id'])
        with executions._transaction() as conn:
            conn.execute(
                "UPDATE executions SET process_id=?, pid=?, process_started_at=? WHERE id=?",
                ('dead-owner', 999999, '0', execution['id']),
            )
        params = {'job_id': job['id'], 'request_id': execution['id'], 'extra_prompt': None}
        journal = scheduler_authority.journal_path(job['id'], execution['id'])
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({'params': params, 'job': job, 'receipt': None}))
        executions.recover_interrupted_executions()
        assert executions.get_execution(execution['id'])['status'] == 'unknown'
        peer(monkeypatch, lambda method, params: {'status': 'terminal', 'job': job,
                                                 'result': [True, 'document', 'answer', None]})
        scheduler_authority.reconcile_pending()
        assert executions.get_execution(execution['id'])['status'] == 'completed'
        assert jobs.get_job(job['id'])['state'] == 'scheduled'
        assert not journal.exists()


def test_attach_lost_capture_stays_on_receipt_path(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        execution = executions.create_execution(job['id'], source='test')
        with executions._transaction() as conn:
            conn.execute(
                "UPDATE executions SET status='unknown', body_phase='attach_lost', finished_at=? WHERE id=?",
                (jobs._hermes_now().isoformat(), execution['id']),
            )
        params = {'job_id': job['id'], 'request_id': execution['id'], 'extra_prompt': None}
        journal = scheduler_authority.journal_path(job['id'], execution['id'])
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({
            'params': params, 'job': job,
            'receipt': None,
            'worker_result': {'result': [True, 'worker', 'worker', None], 'job_flags': {}},
        }))
        peer(monkeypatch, lambda method, params: {'status': 'terminal', 'job': job,
                                                 'result': [True, 'receipt', 'receipt', None]})
        scheduler_authority.reconcile_pending()
        assert executions.get_execution(execution['id'])['status'] == 'completed'
        assert not journal.exists()
        assert journal.with_name(journal.name + '.unsettled').exists()


def test_definite_refusal_is_booked_and_journal_retired(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        execution = executions.create_execution(job['id'], source='test')
        def call(method, params):
            if method == 'cron.submit':
                raise gateway_client.GatewayRPCError('invalid_workdir')
            assert method == 'cron.recover'
            return {'status': 'missing', 'result': None}
        peer(monkeypatch, call)
        result = scheduler_authority.run_canonical_job(job, execution_id=execution['id'])
        assert result[0] is False and 'invalid_workdir' in result[3]
        # Recovery must also finish a refusal if the scheduler exited before its normal tail.
        with executions._transaction() as conn:
            conn.execute('UPDATE executions SET process_id=?,pid=? WHERE id=?', ('old-owner', 999999, execution['id']))
        scheduler_authority.reconcile_pending()
        assert executions.get_execution(execution['id'])['status'] == 'failed'
        assert jobs.get_job(job['id'])['state'] != 'paused'
        assert not scheduler_authority.journal_path(job['id'], execution['id']).exists()


def test_in_process_firer_keeps_its_row_journal_and_output(tmp_path, monkeypatch):
    """The in-gateway ticker fires on pool threads: a tick must not act for a firer still in flight
    in THIS process (770b5924a61's "never this process" fence), nor redo its side effects."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        executions.mark_execution_running(fire)
        params = {'job_id': job['id'], 'request_id': fire, 'extra_prompt': None}
        journal = scheduler_authority.journal_path(job['id'], fire)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({'params': params, 'receipt': {'x': 1}}))
        calls = []
        peer(monkeypatch, lambda method, params: calls.append(method) or {
            'status': 'terminal', 'job': job, 'result': [True, 'document', 'answer', None]})
        scheduler_authority.reconcile_pending()
        assert executions.get_execution(fire)['status'] == 'running'
        assert journal.exists() and calls == []
        assert not list(jobs._job_output_dir(job['id']).glob('*.md'))
        assert jobs.get_job(job['id'])['last_run_at'] is None
        # The firer's own tail still finishes its row with its real outcome.
        assert executions.finish_execution(fire, success=False, error='interrupted') is not None


def test_resume_survives_recovery_of_old_unknown_journal(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        params = {'job_id': job['id'], 'request_id': 'lost-fire', 'extra_prompt': None}
        journal = scheduler_authority.journal_path(job['id'], 'lost-fire')
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({'params': params, 'receipt': None}))
        peer(monkeypatch, lambda method, params: {'status': 'unknown', 'result': None})
        scheduler_authority.reconcile_pending()
        assert jobs.get_job(job['id'])['state'] == 'paused'
        jobs.resume_job(job['id'])
        scheduler_authority.reconcile_pending()
        assert jobs.get_job(job['id'])['enabled'] is True
        assert jobs.get_job(job['id'])['state'] == 'scheduled'
        assert journal.exists()  # uncertain work is observed, never submitted again


@pytest.mark.parametrize('initial', [{}, {'execution_id': None}])
def test_direct_retry_reuses_generated_fire_identity(monkeypatch, initial):
    requests = []
    def lose_reply(method, params):
        requests.append(params['request_id'])
        raise gateway_client.GatewayClientError('disconnected')
    peer(monkeypatch, lose_reply)
    job = {'id': 'direct-fire', **initial}
    for _ in range(2):
        with pytest.raises(scheduler_authority.CronExecutionUnknown):
            scheduler_authority.run_canonical_job(job)
    assert requests[0] == requests[1]


@pytest.mark.parametrize('answer', [
    '  \n', '[CRON_FAILURE]\nchild task failed',
    '[CRON_FAILURE]\n\n```text\nchild task failed\n```',
], ids=['blank', 'declared', 'fenced-declared'])
def test_recovered_receipt_books_the_same_failure_verdicts_as_the_firer(tmp_path, monkeypatch, answer):
    """A blank answer and an agent-declared [CRON_FAILURE] fail the run on the ordinary tail;
    receipt recovery must not book the same result green (ok / delivery_queued -> ok)."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        monkeypatch.setattr('cron.delivery_queue.DELIVERY_DB', tmp_path / 'deliveries.db')
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='telegram:1')
        execution = executions.create_execution(job['id'], source='test')
        executions.mark_execution_running(execution['id'])
        with executions._transaction() as conn:
            conn.execute('UPDATE executions SET process_id=?,pid=? WHERE id=?', ('old-owner', 999999, execution['id']))
        params = {'job_id': job['id'], 'request_id': execution['id'], 'extra_prompt': None}
        journal = scheduler_authority.journal_path(job['id'], execution['id'])
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({'params': params, 'receipt': None}))
        peer(monkeypatch, lambda method, params: {'status': 'terminal', 'job': job,
                                                 'result': [True, 'document', answer, None]})
        scheduler_authority.reconcile_pending()
        saved = jobs.get_job(job['id'])
        assert (saved['last_status'], saved['failure_streak']) == ('error', 1)
        assert executions.get_execution(execution['id'])['status'] == 'failed'
        if answer.startswith('[CRON_FAILURE]'):
            incident, = incidents.list_incidents()
            assert incident['failure_type'] == 'agent'
            assert incident['error'].splitlines()[0] == 'child task failed'
