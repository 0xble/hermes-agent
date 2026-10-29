"""Real canonical RPC, owner SQLite and detached cron worker on a disposable profile.

The loopback provider exercises an actual agent tool, not a patched run_job or
canonical bridge. The process ancestry witness distinguishes worker survival
from survival of an observer which merely waits for gateway-hosted inference.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon


def wait_for(fn, *, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError('timed out waiting for disposable cron fixture')


@pytest.mark.platforms('macos')
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize('lose_worker', [False, True])
def test_canonical_agent_survives_gateway_exit_without_replay(tmp_path, lose_worker):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'profile', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    started, release, effect = (tmp_path / n for n in ('started.json', 'release', 'effect'))
    script = tmp_path / 'tool.py'
    script.write_text("import json,os,time,psutil\nfrom pathlib import Path\n"
        f"Path({str(started)!r}).write_text(json.dumps({{'pid':os.getpid(),"
        "'parents':[p.pid for p in psutil.Process().parents()]}))\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.05)\n"
        f"with Path({str(effect)!r}).open('a') as f: f.write('once\\n')\n"
        "print('TOOL_COMPLETE')\n")
    command = shlex.join([sys.executable, str(script)])
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path != '/v1/chat/completions':
                self.send_error(404, 'Only chat completions are served by this fixture')
                return
            # Metadata probes are not agent inference or an execution replay.
            requests.append(body)
            if any(m['role'] == 'tool' for m in body['messages']):
                message = {'role': 'assistant', 'content': 'CRON_SURVIVAL_COMPLETE'}
                finish = 'stop'
            else:
                message = {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'survival-tool', 'type': 'function', 'function': {'name': 'terminal',
                     'arguments': json.dumps({'command': command, 'timeout': 90})}}]}
                finish = 'tool_calls'
            result = json.dumps({'id': 'survival-fixture', 'object': 'chat.completion',
                'model': 'inert', 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 2, 'completion_tokens': 2, 'total_tokens': 4}}).encode()
            content_type = 'application/json'
            if body.get('stream'):
                delta = dict(message)
                if delta.get('tool_calls'):
                    delta['tool_calls'] = [dict(tool, index=i) for i, tool in enumerate(delta['tool_calls'])]
                chunk = {'id': 'survival-fixture', 'object': 'chat.completion.chunk', 'model': 'inert',
                         'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                result = ('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode()
                content_type = 'text/event-stream'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(result)))
            self.end_headers()
            self.wfile.write(result)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f'http://127.0.0.1:{server.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'inert', 'base_url': url,
                  'api_mode': 'chat_completions'},
        'auxiliary': {'title_generation': {'enabled': False}},
        'platform_toolsets': {'cron': ['terminal']},
        'cron': {'wrap_response': False},
    }))
    (home / '.env').write_text('OPENAI_API_KEY=loopback-only\n')
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
        PYTHONPATH=str(root), OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url,
        HERMES_LAUNCHD_LABEL='ai.hermes-test.canonical-cron', PYTHONUNBUFFERED='1')
    launcher_code = """import json,time
from pathlib import Path
from cron import scheduler
from cron.jobs import create_job
from tools import process_registry
process_registry._is_supervised_gateway_process = lambda: True
job = create_job(prompt='Run the requested inert tool once.', schedule='every 1h', deliver='local',
                 name='canonical survival fixture')
Path('fixture-job.json').write_text(json.dumps(job))
scheduler.run_one_job(job, adapters=None, loop=None)
"""
    launcher = worker_pid = None

    def executions():
        path = home / 'cron' / 'executions.db'
        if not path.exists():
            return None
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='executions'").fetchone() is None:
                return None
            row = db.execute('SELECT * FROM executions ORDER BY claimed_at DESC LIMIT 1').fetchone()
            return dict(row) if row else None

    def admissions():
        with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM session_admissions WHERE principal_id='cron-owner'")]

    try:
        with daemon(root, home, env, barrier=False) as (owner, _):
            with (tmp_path / 'launcher.log').open('w') as log:
                launcher = subprocess.Popen([sys.executable, '-c', launcher_code], cwd=home,
                    env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True)
            try:
                def started_or_terminal():
                    current = executions()
                    return started.exists() or (current and current['status'] not in ('claimed', 'running'))

                wait_for(started_or_terminal)
                assert started.exists(), 'execution became terminal before reaching the agent tool'
            except AssertionError as exc:
                logs = {str(p.relative_to(tmp_path)): p.read_text(errors='replace')[-12000:]
                        for p in tmp_path.rglob('*.log')}
                raise AssertionError(f'{exc}\nlauncher={launcher.poll()}\nlogs={logs}\nrequests={requests}') from exc
            row = executions()
            assert row and row['owner_kind'] == 'detached', row
            worker_pid = row['pid']
            witness = json.loads(started.read_text())
            assert worker_pid in witness['parents'], (row, witness, owner.pid)
            assert owner.pid not in witness['parents'], 'canonical agent still ran in gateway'
            original = admissions()
            assert len(original) == 1 and original[0]['status'] == 'started', original
            assert os.getpgid(worker_pid) != os.getpgid(launcher.pid)
            owner.kill()
            owner.wait(timeout=10)
            os.killpg(launcher.pid, signal.SIGTERM)
            launcher.wait(timeout=10)
            import psutil
            assert psutil.pid_exists(worker_pid)
            if lose_worker:
                os.killpg(worker_pid, signal.SIGKILL)
                # The unknown path must not replay an agent or its pending tool.
                wait_for(lambda: not psutil.pid_exists(worker_pid)
                    or psutil.Process(worker_pid).status() == psutil.STATUS_ZOMBIE)
        with daemon(root, home, env, barrier=False) as (_, _desc):
            if lose_worker:
                rows = wait_for(lambda: admissions() if admissions()[0]['status'] == 'terminal' else None)
                assert rows[0]['outcome'] == 'interrupted'
                release.write_text('go')
                assert not effect.exists()
                assert len(requests) == 1
            else:
                release.write_text('go')
                done = wait_for(lambda: executions() if executions()['status'] == 'completed' else None)
                assert done['id'] == row['id'] and done['pid'] == worker_pid

                # No cron.status poll: the replacement ticker must settle the
                # captured completion before cleaning up the terminal execution.
                settled = wait_for(lambda: admissions() if admissions()[0]['status'] == 'terminal' else None)
                assert settled[0]['outcome'] == 'completed', settled
                with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as db:
                    assert db.execute("SELECT COUNT(*) FROM worker_executions WHERE status!='terminal'").fetchone()[0] == 0
                assert effect.read_text().splitlines() == ['once']
                assert len(requests) == 2
            rows = admissions()
            assert len(rows) == 1 and rows[0]['admission_id'] == original[0]['admission_id']
        with daemon(root, home, env, barrier=False):
            assert len(admissions()) == 1
            assert len(requests) == (1 if lose_worker else 2)
        print(json.dumps({'gateway_pid': owner.pid, 'worker_pid': worker_pid,
                         'admission_id': original[0]['admission_id'],
                         'tool_ancestors': witness['parents'], 'model_calls': len(requests),
                         'interrupted_without_replay': lose_worker}))
    finally:
        release.touch()
        if launcher is not None and launcher.poll() is None:
            os.killpg(launcher.pid, signal.SIGKILL)
            launcher.wait(timeout=10)
        if worker_pid:
            import psutil
            try:
                process = psutil.Process(worker_pid)
                if process.status() != psutil.STATUS_ZOMBIE:
                    os.killpg(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except psutil.NoSuchProcess:
                pass
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.asyncio
async def test_cron_worker_registration_still_requires_adoption_capability(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from gateway import session_worker
    from gateway.session_contract import Principal, SessionRef
    from hermes_state_runtime import RuntimeStoreError
    from tests.cron.test_cron_recovery import _owner

    owner, db = _owner(tmp_path, monkeypatch)
    monkeypatch.setattr(session_worker, '_claim', lambda *args: 'producer-claim')
    connection = SimpleNamespace(authority=owner, actor=Principal(
        'human', str(tmp_path), frozenset({'session:control'}), 'test'))
    params = {'profile_id': str(tmp_path), 'session_id': 'cron-session', 'execution_id': 'cron-worker:fire',
              'generation': 1, 'pid': 1, 'birth': 1.0, 'secret': 'x' * 32, 'kind': 'cron'}
    try:
        with pytest.raises(RuntimeStoreError, match='permission_denied'):
            await session_worker.worker_request(connection, SessionRef(str(tmp_path), 'cron-session'),
                                                 params, operation='register')
        count = db._read_one('SELECT COUNT(*) FROM worker_executions')
        assert count is not None and count[0] == 0
    finally:
        db.close()


@pytest.fixture
def detached_observation(tmp_path, monkeypatch):
    from cron import executions, scheduler_authority
    from hermes_state_keys import CRON_PRINCIPAL_ID

    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', tmp_path / 'executions.db')
    execution = executions.create_execution('observe-job', source='test')
    fire = execution['id']
    assert executions.mark_execution_handoff_pending(fire)
    assert executions.adopt_claimed_execution(fire)
    row = {'request_id': f'cron:observe-job:{fire}', 'principal_id': CRON_PRINCIPAL_ID,
           'target_session_id': 'observe-session', 'admission_id': 'observe-admission'}
    path = tmp_path / 'receipt.json'
    monkeypatch.setattr(scheduler_authority, 'journal_path', lambda *args: path)
    record = {'params': {'job_id': 'observe-job', 'request_id': fire},
              'receipt': {'session_id': row['target_session_id'],
                          'admission_id': row['admission_id']}}
    path.write_text(json.dumps(record))
    return row, path, record


@pytest.mark.parametrize('publication', ['existing', 'race', 'live', 'invalid', 'invalid-race'])
def test_observer_captures_validated_result(detached_observation, monkeypatch, publication):
    from cron import detached_authority, executions
    from hermes_state_runtime import RuntimeStoreError

    row, path, record = detached_observation
    fire = row['request_id'].split(':', 2)[2]
    result = {'result': [True, '# Output', 'CAPTURED', None], 'job_flags': {}}
    if publication.startswith('invalid'):
        result['result'][0] = 'not a bool'

    def publish():
        record['worker_result'] = result
        path.write_text(json.dumps(record))

    if publication in ('existing', 'invalid'):
        publish()

    actual_execution = detached_authority.detached_execution
    checks = []

    def check(job_id, execution_id):
        if publication == 'live':
            current = actual_execution(job_id, execution_id)
            assert current is not None
            checks.append(current)
            publish()
            return current
        # The initial result read already missed. Publish before terminal commit,
        # then let the real execution check observe the now-terminal ledger row.
        publish()
        assert executions.finish_execution(execution_id, success=True)
        current = actual_execution(job_id, execution_id)
        assert current is None
        checks.append(current)
        return current

    monkeypatch.setattr(detached_authority, 'detached_execution', check)
    if publication.startswith('invalid'):
        with pytest.raises(RuntimeStoreError, match='^invalid_cron_result$'):
            asyncio.run(detached_authority.observe_detached(row))
    else:
        assert asyncio.run(detached_authority.observe_detached(row)) == result
    assert len(checks) == (0 if publication in ('existing', 'invalid') else 1)
    assert executions.get_execution(fire)['status'] == (
        'completed' if publication in ('race', 'invalid-race') else 'running')


@pytest.mark.parametrize('worker', ['dead', 'missing', 'terminal'])
def test_observer_lost_worker_logs_identity(detached_observation, monkeypatch, caplog, worker):
    import logging
    from cron import detached_authority, executions
    from hermes_state_runtime import RuntimeStoreError

    row, _, _ = detached_observation
    fire = row['request_id'].split(':', 2)[2]
    execution = executions.get_execution(fire)
    if worker == 'missing':
        with executions._transaction() as conn:
            conn.execute('DELETE FROM executions WHERE id=?', (fire,))
    elif worker == 'terminal':
        assert executions.finish_execution(fire, success=True)
    else:
        with executions._transaction() as conn:
            conn.execute('UPDATE executions SET process_id=? WHERE id=?', ('dead-worker', fire))
        execution['process_id'] = 'dead-worker'
        monkeypatch.setattr(executions, '_owner_identity', lambda *args: 'dead')

    actual_result = detached_authority.detached_result
    reads = []

    def read(current):
        result = actual_result(current)
        reads.append(result)
        return result

    monkeypatch.setattr(detached_authority, 'detached_result', read)
    with caplog.at_level(logging.WARNING, logger='cron.detached_authority'):
        with pytest.raises(RuntimeStoreError, match='^detached_cron_worker_lost$'):
            asyncio.run(detached_authority.observe_detached(row))
    assert reads == [None, None]
    diagnostics = [r for r in caplog.records if r.name == 'cron.detached_authority']
    assert len(diagnostics) == 1 and diagnostics[0].levelno == logging.WARNING
    message = diagnostics[0].getMessage()
    expected = {'admission_id': row['admission_id'], 'job_id': 'observe-job',
                'execution_id': fire, 'status': 'absent' if worker == 'missing' else (
                    'completed' if worker == 'terminal' else 'running'),
                'owner_liveness': {'dead': 'dead', 'missing': 'absent', 'terminal': 'live'}[worker]}
    expected.update({key: None if worker == 'missing' else execution[key]
                     for key in ('pid', 'process_id', 'process_started_at')})
    for key, value in expected.items():
        assert f'{key}={value}' in message


def test_observer_teardown_is_not_explicit_cron_cancel(monkeypatch):
    from cron import detached_authority
    observed = threading.Event()

    def result(row):
        observed.set()

    def unwanted_cancel(row):
        raise AssertionError('observer teardown must not cancel independent execution')

    monkeypatch.setattr(detached_authority, 'detached_result', result)
    monkeypatch.setattr(detached_authority, 'detached_execution', lambda *a: {'pid': 1})
    monkeypatch.setattr(detached_authority, 'cancel_detached', unwanted_cancel)

    async def probe():
        task = asyncio.create_task(detached_authority.observe_detached({'request_id': 'cron:job:fire'}))
        assert await asyncio.to_thread(observed.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(probe())


def test_explicit_cancel_remains_durable_after_owner_recovery(tmp_path, monkeypatch):
    from cron import jobs, scheduler_authority
    from cron.detached_authority import _cancel_path
    from gateway import session_cron
    from hermes_state_runtime import claim_session_input, begin_runtime_epoch, recover_session_inputs
    from tests.cron.test_cron_recovery import _owner
    owner, db = _owner(tmp_path, monkeypatch)
    try:
        with jobs.use_cron_store(tmp_path):
            job = jobs.create_job(prompt='inert', schedule='every 1h', deliver='local')

            async def probe():
                params = {'job_id': job['id'], 'request_id': 'cancel-fire', 'extra_prompt': None}
                receipt = await session_cron.operation(owner, 'submit', params)
                row = claim_session_input(db, epoch=owner.epoch, session_id=receipt['session_id'])
                journal = scheduler_authority.journal_path(job['id'], 'cancel-fire')
                journal.parent.mkdir(parents=True, exist_ok=True)
                journal.write_text(json.dumps({'params': params, 'receipt': receipt}))
                owner.epoch = begin_runtime_epoch(db, instance_id='replacement')
                recover_session_inputs(db, epoch=owner.epoch)
                assert (await session_cron.operation(owner, 'cancel', receipt)) == {'ok': True}
                assert _cancel_path(job['id'], 'cancel-fire').exists()
                assert session_cron._cancellations(owner)[row['admission_id']].is_set()
            asyncio.run(probe())
    finally:
        db.close()


def test_detached_executor_uses_the_admitted_policy(tmp_path, monkeypatch):
    from cron import detached_authority, jobs, scheduler_authority
    from gateway import session_cron
    from hermes_state_runtime import claim_session_input
    from tests.cron.test_cron_recovery import _owner

    owner, db = _owner(tmp_path, monkeypatch)
    actual_operation = session_cron.operation
    executed = []

    async def operation(authority, op, params, actor=None):
        value = await actual_operation(authority, op, params, actor)
        if op == 'submit':
            session_id = value['session_id']
            assert isinstance(session_id, str)
            claim_session_input(db, epoch=owner.epoch, session_id=session_id)
        return value

    def execute(job, receipt, state, record, save, **kwargs):
        executed.append(job['prompt'])
        assert kwargs['extra_prompt'] == 'admitted extra'
        value = (True, 'output', 'answer', None)
        record['worker_result'] = {'result': list(value), 'job_flags': {'_model_unreachable': True}}
        save()
        return value

    monkeypatch.setattr(session_cron, 'operation', operation)
    monkeypatch.setattr(detached_authority, 'owns_detached_execution', lambda *a: True)
    monkeypatch.setattr(detached_authority, 'run_detached', execute)
    try:
        with jobs.use_cron_store(tmp_path):
            admitted = jobs.create_job(prompt='admitted prompt', schedule='every 1h', deliver='local')
            caller = dict(admitted, prompt='mutable caller prompt')

            async def probe():
                session_cron.bind_owner(owner)
                try:
                    result = await asyncio.to_thread(scheduler_authority.run_canonical_job, caller,
                        execution_id='frozen-fire', extra_prompt='admitted extra')
                    assert result == (True, 'output', 'answer', None)
                finally:
                    session_cron.unbind_owner(owner)

            asyncio.run(probe())
            assert executed == ['admitted prompt']
            assert caller['_model_unreachable'] is True
    finally:
        db.close()


def test_captured_detached_completion_is_adopted_not_replayed(tmp_path, monkeypatch):
    from cron import jobs, scheduler_authority
    from cron.detached_authority import recover_detached
    from gateway import session_cron
    from gateway.session_results import admission_result
    from hermes_state_runtime import (claim_session_input, begin_runtime_epoch,
                                     recover_session_inputs, get_session_admission)
    from tests.cron.test_cron_recovery import _owner
    owner, db = _owner(tmp_path, monkeypatch)
    try:
        with jobs.use_cron_store(tmp_path):
            job = jobs.create_job(prompt='inert', schedule='every 1h', deliver='local')

            async def probe():
                params = {'job_id': job['id'], 'request_id': 'completed-fire', 'extra_prompt': None}
                receipt = await session_cron.operation(owner, 'submit', params)
                row = claim_session_input(db, epoch=owner.epoch, session_id=receipt['session_id'])
                journal = scheduler_authority.journal_path(job['id'], 'completed-fire')
                journal.parent.mkdir(parents=True, exist_ok=True)
                result = [True, '# Output', 'ONE_RESULT', None]
                journal.write_text(json.dumps({'params': params, 'receipt': receipt,
                    'worker_result': {'result': result, 'job_flags': {}}}))
                owner.epoch = begin_runtime_epoch(db, instance_id='replacement')
                recover_session_inputs(db, epoch=owner.epoch)
                lost = get_session_admission(db, admission_id=row['admission_id'])
                assert lost['status'] == 'unknown'
                assert recover_detached(db, owner.epoch, lost)
                settled = get_session_admission(db, admission_id=row['admission_id'])
                assert settled['status'] == 'terminal' and settled['outcome'] == 'completed'
                assert admission_result(db, row['admission_id'])['result']['cron_result'] == result
                assert recover_detached(db, owner.epoch, settled)
                assert json.loads(journal.read_text())['canonical_settled'] is True
                assert len(db._read_all('SELECT * FROM session_admissions')) == 1
            asyncio.run(probe())
    finally:
        db.close()
