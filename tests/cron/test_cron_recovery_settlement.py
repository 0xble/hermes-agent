"""A captured completion is imported once, then its recovery journal is retired."""
import asyncio
from unittest.mock import Mock

import pytest

from cron import delivery_queue, executions, jobs, scheduler, scheduler_authority, scheduler_bookkeeping
from gateway import session_cron
from gateway.session_results import admission_result
from hermes_state_runtime import claim_session_input, settle_session_input
from tests.cron.test_canonical_receipt_bookkeeping import peer
from tests.cron.test_cron_recovery import _owner
from tests.cron.test_detached_completion_recovery import _journal


@pytest.mark.parametrize('admission', ['started', 'matching', 'different', 'no-result', 'live'])
def test_captured_terminal_execution_retires_without_rebooking(tmp_path, monkeypatch, admission):
    owner, db = _owner(tmp_path, monkeypatch)
    compose = Mock(wraps=scheduler._save_compose_deliver)
    finish = Mock(wraps=scheduler_bookkeeping.finish_completed_run)
    monkeypatch.setattr(scheduler, '_save_compose_deliver', compose)
    monkeypatch.setattr(scheduler_bookkeeping, 'finish_completed_run', finish)
    try:
        with jobs.use_cron_store(tmp_path):
            job = jobs.create_job(prompt='inert', schedule='every 1h', deliver='local')
            fire = executions.create_execution(job['id'], source='test')['id']
            executions.mark_execution_running(fire)

            async def probe():
                receipt = await session_cron.operation(owner, 'submit', {
                    'job_id': job['id'], 'request_id': fire, 'extra_prompt': None})
                row = claim_session_input(db, epoch=owner.epoch, session_id=receipt['session_id'])
                captured = [True, 'REAL OUTPUT', 'CAPTURED', None]
                journal = _journal(job, fire, receipt, result=captured)
                if admission not in ('started', 'live'):
                    result = ({'result': {'cron_result': captured}, 'usage': {}}
                              if admission == 'matching' else
                              {'result': {'cron_result': [False, '', '', 'finished']}, 'usage': {}}
                              if admission == 'different' else None)
                    settle_session_input(db, epoch=owner.epoch, admission_id=row['admission_id'],
                                         generation=row['generation'], outcome='completed', result=result)
                if admission != 'live':
                    executions.finish_execution(fire, success=True)
                before = jobs.get_job(job['id'])
                saved_journal = journal.read_bytes()
                session_cron.bind_owner(owner)
                try:
                    for _ in range(2):
                        await asyncio.to_thread(scheduler_authority.reconcile_pending, allow_connect=False)
                        if admission == 'live':
                            assert journal.read_bytes() == saved_journal
                        else:
                            assert not journal.exists()
                    if admission == 'started':
                        assert admission_result(db, row['admission_id'])['result']['cron_result'] == captured
                    assert jobs.get_job(job['id']) == before
                    compose.assert_not_called()
                    finish.assert_not_called()
                    assert not list(jobs._job_output_dir(job['id']).glob('*.md'))
                finally:
                    session_cron.unbind_owner(owner)
            asyncio.run(probe())
    finally:
        db.close()


@pytest.mark.parametrize('output,booked', [('REAL OUTPUT', False), ('', False), ('REAL OUTPUT', True)])
def test_captured_completion_without_execution_books_at_most_once(tmp_path, monkeypatch, output, booked):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(delivery_queue, 'DELIVERY_DB', tmp_path / 'deliveries.db')
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='inert', schedule='every 1h', deliver='telegram:1')
        fire = 'pruned-execution'
        captured = [True, output, 'CAPTURED', None]
        journal = _journal(job, fire, None, result=captured)
        original = jobs.save_job_output(job['id'], 'KEEP REAL OUTPUT')
        if booked:
            jobs.mark_job_run(job['id'], True, execution_id=fire)
        peer(monkeypatch, lambda method, params: {'status': 'terminal', 'job': job, 'result': captured})
        save = Mock(wraps=scheduler.save_job_output)
        compose = Mock(wraps=scheduler._save_compose_deliver)
        finish = Mock(wraps=scheduler_bookkeeping.finish_completed_run)
        enqueue = Mock(wraps=delivery_queue.enqueue)
        monkeypatch.setattr(scheduler, 'save_job_output', save)
        monkeypatch.setattr(scheduler, '_save_compose_deliver', compose)
        monkeypatch.setattr(scheduler_bookkeeping, 'finish_completed_run', finish)
        monkeypatch.setattr(delivery_queue, 'enqueue', enqueue)
        scheduler_authority.reconcile_pending()
        assert not journal.exists()
        completed = jobs.get_job(job['id'])
        scheduler_authority.reconcile_pending()
        assert jobs.get_job(job['id']) == completed
        assert completed['repeat']['completed'] == 1
        assert compose.call_count == (0 if booked else 1)
        assert finish.call_count == (0 if booked else 1)
        assert enqueue.call_count == (0 if booked else 1)
        assert save.call_count == int(bool(output) and not booked)
        if not output or booked:
            assert original.read_text() == 'KEEP REAL OUTPUT'
            assert list(jobs._job_output_dir(job['id']).glob('*.md')) == [original]
