"""Admission uncertainty owns only a pause it created, never a pre-existing hold."""
import json
import time

import pytest

from cron import executions, jobs, scheduler_authority
from tests.cron.test_cron_recover_pause import _foreign_journal, _peer, _unknown_fire


def _pause(job):
    return job['state'], job['enabled'], job.get('paused_at'), job.get('paused_reason')


@pytest.mark.parametrize('status', ['missing', 'unknown'])
@pytest.mark.parametrize('settlement', ['missing', 'terminal'])
@pytest.mark.parametrize('preexisting', [True, False])
def test_recovery_never_claims_a_preexisting_pause(tmp_path, monkeypatch, status, settlement, preexisting):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        executions.mark_execution_running(fire)
        journal = _foreign_journal(job, fire)
        record = json.loads(journal.read_text())
        record['submit_started_at'] = time.time() - scheduler_authority.MISSING_SUBMIT_GRACE_SECONDS - 1
        journal.write_text(json.dumps(record))
        original = _pause(jobs.pause_job(job['id'], reason='operator hold')) if preexisting else None
        _peer(monkeypatch, lambda method: {'status': status, 'result': None})

        scheduler_authority.reconcile_pending()
        observed = [_pause(jobs.get_job(job['id']))]
        recorded = json.loads(journal.read_text()).get('pause_recorded')
        scheduler_authority.reconcile_pending()
        observed.append(_pause(jobs.get_job(job['id'])))
        if status == 'unknown':
            assert journal.exists() and executions.get_execution(fire)['status'] == 'running'

        # Unknown is not evidence of absence; only later missing or terminal reads settle it.
        if journal.exists():
            answer = ({'status': 'missing', 'result': None} if settlement == 'missing' else
                      {'status': 'terminal', 'job': job, 'result': [True, 'document', 'answer', None]})
            _peer(monkeypatch, lambda method: answer)
            scheduler_authority.reconcile_pending()
            scheduler_authority.reconcile_pending()
        assert not journal.exists()
        expected_status = 'failed' if status == 'missing' or settlement == 'missing' else 'completed'
        assert executions.get_execution(fire)['status'] == expected_status
        saved = jobs.get_job(job['id'])
        if preexisting:
            assert _pause(saved) == original
            assert all(pause == original for pause in observed)
            assert not recorded, 'recovery must not record ownership of another pause'
        else:
            assert observed[0][0:2] == ('paused', False)
            assert recorded['paused_at'] == observed[0][2]
            assert (saved['state'], saved['enabled'], saved['paused_reason']) == ('scheduled', True, None)


@pytest.mark.parametrize('preexisting', [True, False])
def test_firer_uncertainty_preserves_an_existing_pause(tmp_path, monkeypatch, preexisting):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        original = _pause(jobs.pause_job(job['id'], reason='operator hold')) if preexisting else None
        journal = _unknown_fire(monkeypatch, job, fire)
        held = _pause(jobs.get_job(job['id']))
        scheduler_authority.pause_for_unknown(job['id'], fire, 'second uncertainty observer')
        repeated = _pause(jobs.get_job(job['id']))
        _peer(monkeypatch, lambda method: {'status': 'terminal', 'job': job,
                                           'result': [True, 'document', 'answer', None]})
        scheduler_authority.reconcile_pending()
        assert not journal.exists()
        assert executions.get_execution(fire)['status'] == 'completed'
        saved = jobs.get_job(job['id'])
        if preexisting:
            assert _pause(saved) == original
            assert held == repeated == original
        else:
            assert held == repeated, 'repeat uncertainty must keep its existing ownership token'
            assert (saved['state'], saved['enabled'], saved['paused_reason']) == ('scheduled', True, None)
