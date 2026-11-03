"""A missing recovery snapshot cannot retract a submit still waiting to commit."""
import asyncio
import json

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from cron import executions, jobs, scheduler_authority
from gateway import session_cron
from hermes_cli import gateway_client
from hermes_state_runtime import claim_session_input, settle_session_input
from tests.cron.test_cron_recovery import _owner


@pytest.mark.asyncio
@pytest.mark.parametrize('commit_later', [True, False])
async def test_ambiguous_submit_keeps_its_journal_until_verified(tmp_path, monkeypatch, caplog, commit_later):
    authority, db = _owner(tmp_path, monkeypatch)
    submitted, disconnected, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests, commits = [], []
    real_rpc = gateway_client.GatewayClient.rpc

    async def rpc(client, method, **params):
        # Only the held submit has a short deadline; owner/SQLite reads keep their real budget.
        return await real_rpc(client, method, _timeout=.01 if method == 'cron.submit' else 30, **params)

    monkeypatch.setattr(gateway_client.GatewayClient, 'rpc', rpc)

    async def handler(ws):
        async for raw in ws:
            request = json.loads(raw)
            method, params = request['method'], request['params']
            requests.append((ws, method, params))
            if method == 'cron.submit':
                submitted.set()

                async def closed():
                    await ws.wait_closed()
                    disconnected.set()

                watcher = asyncio.create_task(closed())
                try:
                    await release.wait()
                    if not commit_later:
                        return
                    receipt = await session_cron.operation(authority, 'submit', params)
                    commits.append(receipt)
                    row = claim_session_input(db, epoch=authority.epoch, session_id=receipt['session_id'])
                    settle_session_input(db, epoch=authority.epoch, admission_id=row['admission_id'],
                        generation=row['generation'], outcome='completed',
                        result={'result': {'cron_result': [True, 'document', 'answer', None]}, 'usage': {}})
                    answer = receipt
                finally:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
            else:
                answer = await session_cron.operation(authority, method.removeprefix('cron.'), params)
            try:
                await ws.send(json.dumps({'id': request['id'], 'result': answer}))
            except ConnectionClosed:
                return

    def fire(job, execution_id):
        try:
            return scheduler_authority.run_canonical_job(job, execution_id=execution_id)
        except scheduler_authority.CronExecutionUnknown as exc:
            return exc

    task = None
    try:
        with jobs.use_cron_store(tmp_path / 'cron'):
            job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
            execution_id = executions.create_execution(job['id'], source='test')['id']
            executions.mark_execution_running(execution_id)
            # Recovery must be allowed to inspect this fire while its original submit is in flight.
            with executions._transaction() as conn:
                conn.execute('UPDATE executions SET process_id=?,pid=? WHERE id=?',
                             ('departed-firer', 999999, execution_id))
            journal = scheduler_authority.journal_path(job['id'], execution_id)
            async with serve(handler, '127.0.0.1', 0) as server:
                monkeypatch.setenv('HERMES_TUI_GATEWAY_URL',
                    f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}')
                task = asyncio.create_task(asyncio.to_thread(fire, job, execution_id))
                try:
                    await asyncio.wait_for(submitted.wait(), 5)
                    await asyncio.wait_for(disconnected.wait(), 5)
                    # A second connection overtakes the blocked submit and sees no admission.
                    await asyncio.to_thread(scheduler_authority.reconcile_pending)
                    assert journal.exists(), 'missing is a snapshot, not proof of never-admission'
                    assert executions.get_execution(execution_id)['status'] == 'running'
                    assert 'never admitted' not in caplog.text
                    if commit_later:
                        release.set()
                    result = await asyncio.wait_for(asyncio.shield(task), 30)
                    if commit_later:
                        assert result == (True, 'document', 'answer', None)
                        assert len(commits) == 1
                        assert db._read_one('SELECT COUNT(*) FROM session_admissions')[0] == 1
                        await asyncio.to_thread(scheduler_authority.reconcile_pending)
                        assert executions.get_execution(execution_id)['status'] == 'completed'
                        assert not journal.exists()
                    else:
                        assert isinstance(result, scheduler_authority.CronExecutionUnknown)
                        assert journal.exists()
                        assert job['id'] in str(result) and execution_id in str(result)
                        assert 'cron.submit' in str(result) and 'answer' in str(result)
                        # One reconciler read plus the bounded fresh-link recovery attempts.
                        recoveries = [r for r in requests if r[1] == 'cron.recover']
                        assert 2 <= len(recoveries) <= 6
                    submits = [r for r in requests if r[1] == 'cron.submit']
                    assert len(submits) == 1
                    assert all(r[0] is not submits[0][0] for r in requests if r[1] == 'cron.recover')
                    assert all(r[2]['request_id'] == execution_id for r in requests if r[1] == 'cron.recover')
                    assert 'Gateway connection failed' not in caplog.text
                finally:
                    release.set()
                    if task is not None:
                        await asyncio.wait_for(asyncio.shield(task), 30)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_lost_status_answer_rereads_the_exact_admission(tmp_path, monkeypatch):
    authority, db = _owner(tmp_path, monkeypatch)
    requests = []
    real_rpc = gateway_client.GatewayClient.rpc
    status_attempts = 0

    async def rpc(client, method, **params):
        nonlocal status_attempts
        if method == 'cron.status':
            status_attempts += 1
        timeout = .01 if method == 'cron.status' and status_attempts == 1 else 30
        return await real_rpc(client, method, _timeout=timeout, **params)

    monkeypatch.setattr(gateway_client.GatewayClient, 'rpc', rpc)

    async def handler(ws):
        async for raw in ws:
            request = json.loads(raw)
            method, params = request['method'], request['params']
            requests.append((ws, method, params))
            if method == 'cron.status' and len(requests) == 2:
                row = claim_session_input(db, epoch=authority.epoch, session_id=params['session_id'])
                settle_session_input(db, epoch=authority.epoch, admission_id=row['admission_id'],
                    generation=row['generation'], outcome='completed',
                    result={'result': {'cron_result': [True, 'document', 'answer', None]}, 'usage': {}})
                # Status committed no work; its lost answer only authorizes another exact read.
                await ws.wait_closed()
                return
            answer = await session_cron.operation(authority, method.removeprefix('cron.'), params)
            await ws.send(json.dumps({'id': request['id'], 'result': answer}))

    try:
        with jobs.use_cron_store(tmp_path / 'cron'):
            job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
            fire = executions.create_execution(job['id'], source='test')['id']
            async with serve(handler, '127.0.0.1', 0) as server:
                monkeypatch.setenv('HERMES_TUI_GATEWAY_URL',
                    f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}')
                result = await asyncio.wait_for(asyncio.to_thread(
                    scheduler_authority.run_canonical_job, job, execution_id=fire), 30)
            assert result == (True, 'document', 'answer', None)
            assert [method for _, method, _ in requests] == ['cron.submit', 'cron.status', 'cron.status']
            assert requests[1][0] is not requests[2][0]
            assert requests[1][2] == requests[2][2]
            assert set(requests[2][2]) == {'admission_id', 'session_id'}
            assert db._read_one('SELECT COUNT(*) FROM session_admissions')[0] == 1
    finally:
        db.close()
