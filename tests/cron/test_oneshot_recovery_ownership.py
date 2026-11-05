"""Canonical recovery owns only its exact one-shot occurrence, never a replacement arm."""
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cron import executions, incidents, jobs, jobs_oneshot, scheduler, scheduler_authority as authority
from hermes_cli.cron import cron_resume
from tests.cron import oneshot_helpers
from tests.cron.oneshot_helpers import NOW
from tests.cron.test_cron_recover_pause import _foreign_journal, _peer

store = oneshot_helpers.store


def _started(*, lateness=60):
    due = (NOW - timedelta(seconds=lateness)).isoformat()
    job = jobs.create_job(prompt='original payload', schedule=NOW.isoformat(), deliver='local')
    assert jobs.claim_dispatch(job['id'])
    rows = jobs.load_jobs()
    rows[0]['next_run_at'] = rows[0]['schedule']['run_at'] = due
    jobs.save_jobs(rows)
    job = jobs.get_job(job['id'])
    row = executions.create_execution(job['id'], source='scheduler', scheduled_instant=due)
    executions.mark_execution_running(row['id'])
    journal = _foreign_journal(job, row['id'])
    return job, row['id'], journal


def _apply_recovery_evidence(evidence, journal, monkeypatch):
    def capture_settled():
        record = json.loads(journal.read_text())
        record.update(worker_result={'result': [True, 'doc', 'answer', None]}, canonical_settled=True)
        journal.write_text(json.dumps(record))

    def identity_conflict():
        record = json.loads(journal.read_text())
        record['params']['request_id'] = 'other-attempt'
        journal.write_text(json.dumps(record))

    def unreadable():
        original = Path.read_text

        def read(path, *args, **kwargs):
            if path == journal:
                raise PermissionError('fixture journal unreadable')
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'read_text', read)

    handlers = {
        'valid': lambda: None,
        'corrupt': lambda: journal.write_text('{not-json'),
        'capture-settled': capture_settled,
        'identity-conflict': identity_conflict,
        'unreadable': unreadable,
    }
    handlers[evidence]()


@pytest.mark.parametrize('entry', ['expiry', 'limit', 'claim'])
@pytest.mark.parametrize('status', ['running', 'unknown'])
@pytest.mark.parametrize('evidence', ['valid', 'corrupt', 'unreadable', 'capture-settled', 'identity-conflict'])
def test_current_recovery_obligation_blocks_each_destructive_gate(
        store, monkeypatch, entry, status, evidence):
    job, fire, journal = _started(lateness=660 if entry == 'expiry' else 60)
    if status == 'unknown':
        assert executions.terminalize_dead_owner(fire, reason='former owner exited')
    _apply_recovery_evidence(evidence, journal, monkeypatch)
    before = jobs.get_job(job['id'])
    for _ in range(2):
        assert (jobs.claim_dispatch(job['id']) if entry == 'claim' else jobs.get_due_jobs()) in (False, [])
        assert jobs.get_job(job['id']) == before
    assert journal.exists()
    assert executions.get_execution(fire)['status'] == status
    if evidence in {'corrupt', 'unreadable', 'identity-conflict'}:
        with incidents._transaction() as conn:
            rows = conn.execute('SELECT error FROM cron_incidents WHERE job_id=?', (job['id'],)).fetchall()
        assert len(rows) == 1
        assert 'recovery evidence' in rows[0]['error']


@pytest.mark.parametrize('success', [True, False])
@pytest.mark.parametrize('status', ['running', 'unknown'])
def test_current_receipt_settles_once_after_dispatch_and_expiry_gates(store, monkeypatch, success, status):
    job, fire, journal = _started(lateness=660)
    if status == 'unknown':
        assert executions.terminalize_dead_owner(fire, reason='former owner exited')
    assert not jobs.claim_dispatch(job['id'])
    assert jobs.get_due_jobs() == []
    from gateway import session_cron
    monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: None)
    error = None if success else 'terminal canonical failure'
    _peer(monkeypatch, lambda method: {'status': 'terminal', 'job': job,
                                       'result': [success, 'terminal document', 'answer', error]})
    # No person transport: local compose/delivery is inert; exercise the real terminal reducer.
    monkeypatch.setattr(scheduler, '_save_compose_deliver', lambda *args, **kwargs: None)
    authority.reconcile_pending()
    saved = jobs.get_job(job['id'])
    assert saved['state'] == 'completed' and not saved['enabled']
    assert saved['repeat']['completed'] == 1
    assert saved['canonical_completions'] == [fire]
    assert saved['last_status'] == ('ok' if success else 'error')
    assert saved['last_error'] == error
    assert executions.get_execution(fire)['status'] == ('completed' if success else 'failed')
    assert not journal.exists()
    authority.reconcile_pending()
    assert jobs.get_job(job['id']) == saved


@pytest.mark.parametrize('entry', ['expiry', 'limit', 'claim'])
@pytest.mark.parametrize('evidence', ['absent', 'prior-arm'])
def test_only_current_link_can_prevent_dead_partial_effects_retirement(store, entry, evidence):
    job, fire, journal = _started(lateness=660 if entry == 'expiry' else 60)
    if evidence == 'absent':
        journal.unlink()
    else:
        with executions._transaction() as conn:
            conn.execute('UPDATE executions SET scheduled_instant=? WHERE id=?',
                         ((NOW - timedelta(days=1)).isoformat(), fire))
    assert (jobs.claim_dispatch(job['id']) if entry == 'claim' else jobs.get_due_jobs()) in (False, [])
    assert jobs.get_job(job['id']) is None
    assert journal.exists() == (evidence == 'prior-arm')


@pytest.mark.parametrize('success', [True, False])
@pytest.mark.parametrize('entry', ['receipt', 'reducer'])
def test_prior_arm_completion_settles_old_attempt_without_touching_replacement(
        store, monkeypatch, success, entry):
    old, fire, journal = _started()
    jobs.pause_job(old['id'], reason='operator rearm')
    assert cron_resume(SimpleNamespace(job_id=old['id'], run_at=(NOW + timedelta(minutes=5)).isoformat(),
                                       run_now=False)) == 0
    replacement = jobs.get_job(old['id'])
    assert replacement['repeat']['completed'] == 0
    error = None if success else 'old canonical failure'
    if entry == 'reducer':
        assert jobs.mark_job_run(old['id'], success, error, execution_id=fire)
    else:
        from gateway import session_cron
        monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: None)
        _peer(monkeypatch, lambda method: {'status': 'terminal', 'job': old,
                                           'result': [success, 'old document', 'old answer', error]})

        def forbidden(*args, **kwargs):
            pytest.fail('prior-arm receipt attempted output/delivery for a replacement arm')

        monkeypatch.setattr(scheduler, '_save_compose_deliver', forbidden)
        authority.reconcile_pending()
        assert executions.get_execution(fire)['status'] == ('completed' if success else 'failed')
        assert not journal.exists()
        assert 'old document' in '\n'.join(p.read_text() for p in (store / 'output' / old['id']).glob('*.md'))
    assert jobs.get_job(old['id']) == replacement


def test_rearm_during_composition_is_rechecked_at_output_fence(store, monkeypatch):
    old, fire, journal = _started()
    from gateway import session_cron
    monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: None)
    _peer(monkeypatch, lambda method: {'status': 'terminal', 'job': old,
                                       'result': [True, 'old document', 'old answer', None]})
    original = scheduler._save_compose_deliver
    replacement = []

    def rearm_then_compose(*args, **kwargs):
        jobs.pause_job(old['id'], reason='operator rearm during composition')
        assert cron_resume(SimpleNamespace(job_id=old['id'], run_at=(NOW + timedelta(minutes=5)).isoformat(),
                                           run_now=False)) == 0
        replacement.append(jobs.get_job(old['id']))
        return original(*args, **kwargs)

    monkeypatch.setattr(scheduler, '_save_compose_deliver', rearm_then_compose)
    authority.reconcile_pending()
    assert journal.exists()  # Fence loss defers; it must not discard the old result.
    assert jobs.get_job(old['id']) == replacement[0]
    monkeypatch.setattr(scheduler, '_save_compose_deliver', original)
    authority.reconcile_pending()
    assert not journal.exists()
    assert executions.get_execution(fire)['status'] == 'completed'
    assert jobs.get_job(old['id']) == replacement[0]


@pytest.mark.parametrize('status', ['completed', 'failed'])
def test_terminal_canonical_bookkeeping_does_not_pin_recovery_ownership(store, status):
    job, fire, journal = _started()
    assert jobs.mark_job_run(job['id'], status == 'completed', execution_id=fire)
    executions.recover_receipted_execution(fire, job['id'], success=status == 'completed')
    assert not jobs_oneshot.occurrence_recovery_pending(jobs.get_job(job['id']))
    assert journal.exists()


def test_queued_journal_is_not_start_evidence_or_permission_to_send(store):
    due = (NOW - timedelta(minutes=10)).isoformat()
    job = jobs.create_job(prompt='unsent payload', schedule=NOW.isoformat(), deliver='local')
    rows = jobs.load_jobs()
    rows[0]['next_run_at'] = rows[0]['schedule']['run_at'] = due
    jobs.save_jobs(rows)
    job = jobs.get_job(job['id'])
    row = executions.create_execution(job['id'], source='scheduler', scheduled_instant=due)
    journal = _foreign_journal(job, row['id'])
    assert not jobs_oneshot.occurrence_started(job)
    assert jobs.get_due_jobs() == []
    saved = jobs.get_job(job['id'])
    assert saved['state'] == 'missed' and not saved['enabled']
    assert saved['prompt'] == 'unsent payload' and journal.exists()
    assert executions.get_execution(row['id'])['started_at'] is None
