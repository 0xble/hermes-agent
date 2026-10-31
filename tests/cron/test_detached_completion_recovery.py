"""Detached cron completion and cancellation survive the owner's handoff gaps."""
import asyncio
import json

import pytest

from cron import executions, jobs, scheduler_authority
from gateway import session_cron
from gateway.session_results import admission_result
from hermes_state_runtime import adopt_worker_execution, claim_session_input, get_session_admission, mutate_runtime_session, recover_session_inputs, register_worker_execution
from tests.fakes.runtime_owner import begin_runtime_epoch
from tests.cron.test_cron_recovery import _owner


def _journal(job, fire, receipt, *, result=None):
    params = {'job_id': job['id'], 'request_id': fire, 'extra_prompt': None}
    path = scheduler_authority.journal_path(job['id'], fire)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {'params': params, 'receipt': receipt}
    if result is not None:
        record['worker_result'] = {'result': result, 'job_flags': {}}
    path.write_text(json.dumps(record))
    return path


def test_terminal_execution_reconciles_canonical_result_without_status_poll(tmp_path, monkeypatch):
    owner, db = _owner(tmp_path, monkeypatch)
    try:
        with jobs.use_cron_store(tmp_path):
            job = jobs.create_job(prompt='inert', schedule='every 1h', deliver='local')
            fire = executions.create_execution(job['id'], source='direct')['id']
            executions.mark_execution_running(fire)

            async def probe():
                receipt = await session_cron.operation(owner, 'submit', {
                    'job_id': job['id'], 'request_id': fire, 'extra_prompt': None})
                row = claim_session_input(db, epoch=owner.epoch, session_id=receipt['session_id'])
                owner.epoch = begin_runtime_epoch(db, instance_id='replacement')
                recover_session_inputs(db, epoch=owner.epoch)
                result = [True, '# Output', 'CAPTURED_ONCE', None]
                journal = _journal(job, fire, receipt, result=result)
                executions.finish_execution(fire, success=True)
                # The worker already booked delivery and the job. Recovery must only
                # settle the canonical receipt, not compose or spend this fire again.
                before = jobs.get_job(job['id'])
                session_cron.bind_owner(owner)
                try:
                    await asyncio.to_thread(scheduler_authority.reconcile_pending, allow_connect=False)
                    settled = get_session_admission(db, admission_id=row['admission_id'])
                    assert settled['status'] == 'terminal'
                    assert admission_result(db, row['admission_id'])['result']['cron_result'] == result
                    assert not journal.exists()
                    await asyncio.to_thread(scheduler_authority.reconcile_pending, allow_connect=False)
                    assert jobs.get_job(job['id']) == before
                finally:
                    session_cron.unbind_owner(owner)
            asyncio.run(probe())
    finally:
        db.close()


def test_unsettled_terminal_execution_keeps_journal_without_owner(tmp_path, monkeypatch):
    owner, db = _owner(tmp_path, monkeypatch)
    try:
        with jobs.use_cron_store(tmp_path):
            job = jobs.create_job(prompt='inert', schedule='every 1h', deliver='local')
            fire = executions.create_execution(job['id'], source='direct')['id']
            journal = _journal(job, fire, None, result=[True, '# Output', 'CAPTURED', None])
            executions.finish_execution(fire, success=True)
            scheduler_authority.reconcile_pending(allow_connect=False)
            assert journal.exists()
    finally:
        db.close()


@pytest.mark.parametrize('adopt', [False, True])
def test_captured_completion_retires_registered_worker_in_previous_epoch(tmp_path, monkeypatch, adopt):
    from cron.detached_authority import PREFIX, recover_detached
    owner, db = _owner(tmp_path, monkeypatch)
    try:
        with jobs.use_cron_store(tmp_path):
            job = jobs.create_job(prompt='inert', schedule='every 1h', deliver='local')

            async def probe():
                fire = 'captured-worker'
                receipt = await session_cron.operation(owner, 'submit', {
                    'job_id': job['id'], 'request_id': fire, 'extra_prompt': None})
                row = claim_session_input(db, epoch=owner.epoch, session_id=receipt['session_id'])
                scope = dict(execution_id=PREFIX + fire, session_id=receipt['session_id'],
                             generation=row['generation'], adoption_secret='x' * 32)
                register_worker_execution(db, epoch=owner.epoch, kind='cron', **scope)
                if adopt:
                    adopt_worker_execution(db, epoch=owner.epoch, **scope)
                journal = _journal(job, fire, receipt, result=[True, '# Output', 'CAPTURED', None])
                owner.epoch = begin_runtime_epoch(db, instance_id='replacement')
                recover_session_inputs(db, epoch=owner.epoch)
                lost = get_session_admission(db, admission_id=row['admission_id'])
                assert db._read_one('SELECT status FROM worker_executions')[0] == 'unknown'
                assert recover_detached(db, owner.epoch, lost)
                assert db._read_one('SELECT status FROM worker_executions')[0] == 'terminal'
                session = db.get_session(receipt['session_id'])
                changed = mutate_runtime_session(db, epoch=owner.epoch, principal_id='cron-owner',
                    session_id=receipt['session_id'], request_id='after-completion',
                    expected_revision=session['runtime_revision'], expected_generation=session['runtime_generation'],
                    operation='reset', payload={})
                assert changed is not None
                assert json.loads(journal.read_text())['canonical_settled'] is True
            asyncio.run(probe())
    finally:
        db.close()


def test_cancel_before_submit_receipt_reaches_detached_worker(tmp_path, monkeypatch):
    from cron.detached_authority import _cancel_path
    owner, db = _owner(tmp_path, monkeypatch)
    try:
        with jobs.use_cron_store(tmp_path):
            job = jobs.create_job(prompt='inert', schedule='every 1h', deliver='local')

            async def probe():
                fire = 'cancel-before-receipt'
                _journal(job, fire, None)
                receipt = await session_cron.operation(owner, 'submit', {
                    'job_id': job['id'], 'request_id': fire, 'extra_prompt': None})
                row = claim_session_input(db, epoch=owner.epoch, session_id=receipt['session_id'])
                assert await session_cron.operation(owner, 'cancel', receipt) == {'ok': True}
                assert session_cron._cancellations(owner)[row['admission_id']].is_set()
                # The external process cannot see the owner's threading.Event.
                assert _cancel_path(job['id'], fire).exists()
                _journal(job, fire, receipt)
                owner.epoch = begin_runtime_epoch(db, instance_id='replacement')
                recover_session_inputs(db, epoch=owner.epoch)
                assert json.loads(_cancel_path(job['id'], fire).read_text()) == {'cancelled': True}
            asyncio.run(probe())
    finally:
        db.close()
