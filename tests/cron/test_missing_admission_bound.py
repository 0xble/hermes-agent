"""Lost submits eventually settle, but an overtaking missing read is never enough."""
from contextlib import asynccontextmanager
import json
import time

from cron import executions, jobs, scheduler, scheduler_authority
from hermes_cli import gateway_client
from tests.cron.test_cron_recover_pause import _foreign_journal, _peer


def test_missing_submit_requires_age_and_consecutive_reads(tmp_path, monkeypatch):
    _peer(monkeypatch, lambda method: {'status': 'missing', 'result': None})
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        executions.mark_execution_running(fire)
        journal = _foreign_journal(job, fire)
        record = json.loads(journal.read_text())
        record['submit_started_at'] = time.time()
        journal.write_text(json.dumps(record))
        for _ in range(3):
            scheduler_authority.reconcile_pending()
        assert journal.exists()
        assert executions.get_execution(fire)['status'] == 'running'
        assert jobs.get_job(job['id'])['state'] == 'paused'

        # A non-missing observation interrupts the consecutive-missing streak.
        _peer(monkeypatch, lambda method: {'status': 'unknown', 'result': None})
        scheduler_authority.reconcile_pending()
        record = json.loads(journal.read_text())
        record['submit_started_at'] = time.time() - 601
        journal.write_text(json.dumps(record))
        _peer(monkeypatch, lambda method: {'status': 'missing', 'result': None})
        scheduler_authority.reconcile_pending()
        assert journal.exists(), 'age without repeated missing reads is not enough'
        scheduler_authority.reconcile_pending()
        saved = jobs.get_job(job['id'])
        assert executions.get_execution(fire)['status'] == 'failed'
        assert saved['last_status'] == 'error'
        assert 'submit never committed' in saved['last_error'].lower()
        assert '600' in saved['last_error'] and '2' in saved['last_error']
        assert saved['state'] == 'scheduled' and saved['enabled'] is True
        assert not journal.exists()


def test_live_firer_terminal_tail_lifts_its_uncertainty_pause(tmp_path, monkeypatch):
    """A recovery observer can pause an in-flight submit before the live firer gets its receipt."""
    class Peer:
        async def rpc(self, method, **params):
            if method == 'cron.submit':
                scheduler_authority.pause_for_unknown(params['job_id'], params['request_id'], 'uncertain submit')
                return {'session_id': 'session', 'admission_id': 'admission'}
            assert method == 'cron.status'
            return {'status': 'terminal', 'result': [True, 'document', 'answer', None]}

    @asynccontextmanager
    async def connected():
        yield Peer()

    monkeypatch.setattr(gateway_client, 'connect_gateway', connected)
    monkeypatch.setattr(scheduler, 'run_job', lambda job, **kwargs: scheduler_authority.run_canonical_job(
        job, execution_id=kwargs.get('execution_id')))
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        assert scheduler._run_one_job_body(dict(job, execution_id=fire)) is True
        saved = jobs.get_job(job['id'])
        assert executions.get_execution(fire)['status'] == 'completed'
        assert saved['state'] == 'scheduled' and saved['enabled'] is True
        assert saved['paused_reason'] is None
        assert not scheduler_authority.journal_path(job['id'], fire).exists()


def test_legacy_missing_journal_pins_age_before_pause_writes(tmp_path, monkeypatch):
    import os

    _peer(monkeypatch, lambda method: {'status': 'missing', 'result': None})
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        journal = _foreign_journal(job, fire)
        started = time.time() - 601
        os.utime(journal, (started, started))
        scheduler_authority.reconcile_pending()
        assert abs(json.loads(journal.read_text())['submit_started_at'] - started) < .01
        assert journal.exists()
        scheduler_authority.reconcile_pending()
        assert executions.get_execution(fire)['status'] == 'failed'
        assert jobs.get_job(job['id'])['state'] == 'scheduled'
        assert not journal.exists()


def test_admitted_missing_journal_is_never_aged_into_failure(tmp_path, monkeypatch):
    _peer(monkeypatch, lambda method: {'status': 'missing', 'result': None})
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        journal = _foreign_journal(job, fire)
        record = json.loads(journal.read_text())
        record.update(submit_started_at=time.time() - 3600,
                      receipt={'session_id': 'session', 'admission_id': 'admission'})
        journal.write_text(json.dumps(record))
        for _ in range(3):
            scheduler_authority.reconcile_pending()
        assert journal.exists()
        assert executions.get_execution(fire)['status'] != 'failed'
        assert jobs.get_job(job['id'])['state'] == 'paused'


def test_refused_read_breaks_missing_streak(tmp_path, monkeypatch):
    def refused(method):
        raise gateway_client.GatewayRPCError('storage_unavailable')

    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        journal = _foreign_journal(job, fire)
        record = json.loads(journal.read_text())
        record['submit_started_at'] = time.time() - 601
        journal.write_text(json.dumps(record))
        _peer(monkeypatch, lambda method: {'status': 'missing', 'result': None})
        scheduler_authority.reconcile_pending()
        _peer(monkeypatch, refused)
        scheduler_authority.reconcile_pending()
        _peer(monkeypatch, lambda method: {'status': 'missing', 'result': None})
        scheduler_authority.reconcile_pending()
        assert journal.exists()
        assert executions.get_execution(fire)['status'] != 'failed'
        scheduler_authority.reconcile_pending()
        assert executions.get_execution(fire)['status'] == 'failed'
        assert not journal.exists()


def test_submit_save_cannot_lose_a_concurrent_recovery_pause(tmp_path, monkeypatch):
    import threading
    import utils

    real_write = utils.atomic_json_write
    injected = False
    pause_done = threading.Event()
    threads = []

    def write(path, record, **kwargs):
        nonlocal injected
        if record.get('receipt') and not injected:
            injected = True
            def pause():
                params = record['params']
                scheduler_authority.pause_for_unknown(params['job_id'], params['request_id'], 'recovery pause')
                pause_done.set()
            from contextvars import copy_context
            thread = threading.Thread(target=copy_context().run, args=(pause,))
            threads.append(thread)
            thread.start()
            # Before the fix the pause finishes inside this stale save; after the fix it waits
            # on the shared jobs lock and merges its stamp after this atomic replacement.
            pause_done.wait(.5)
        return real_write(path, record, **kwargs)

    class Peer:
        async def rpc(self, method, **params):
            if method == 'cron.submit':
                return {'session_id': 'session', 'admission_id': 'admission'}
            return {'status': 'terminal', 'result': [True, 'document', 'answer', None]}

    @asynccontextmanager
    async def connected():
        yield Peer()

    monkeypatch.setattr(utils, 'atomic_json_write', write)
    monkeypatch.setattr(gateway_client, 'connect_gateway', connected)
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        try:
            assert scheduler_authority.run_canonical_job(job, execution_id=fire)[0] is True
        finally:
            for thread in threads:
                thread.join(5)
        assert pause_done.is_set() and all(not thread.is_alive() for thread in threads)
        record = json.loads(scheduler_authority.journal_path(job['id'], fire).read_text())
        assert record['pause_recorded']['reason'] == 'recovery pause'
        assert jobs.get_job(job['id'])['paused_reason'] == 'recovery pause'
