"""Real LOCAL admission recovery, interrupted model and durable status replay."""
import asyncio
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

from automation_peer import Model
from local_recovery_probe import child_env, daemon, rpc, websocket
from tests.waiting import async_wait_until


def probe(base):
    root = Path(__file__).resolve().parents[3]
    home, user = base / 'state', base / 'user'
    home.mkdir(); user.mkdir()
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    model.requests, model.checkin_turns, model.checkin = [], 0, True
    model.checkin_blocked, model.checkin_release = threading.Event(), threading.Event()
    model.blocked, model.release = threading.Event(), threading.Event()
    model.gate = home / 'unused-terminal-gate'
    threading.Thread(target=model.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{model.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        '_config_version': 12,
        'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'local-fixture', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}},
    }))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               HERMES_TEST_ISOLATION=str(home), OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url,
               PYTHONUNBUFFERED='1')
    pids, epochs = [], []
    sid = None

    def rows():
        with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM session_admissions ORDER BY seq')]

    def ticks():
        return [r for r in rows() if 'supervisor-checkin' in r['request_id']]

    def state():
        with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as db:
            row = db.execute('SELECT value FROM state_meta WHERE key=?', ('checkin:' + sid,)).fetchone()
            return json.loads(row[0]) if row and row[0] else None

    def advance(**fields):
        value = {**state(), **fields}
        with sqlite3.connect(home / 'state.db') as db:
            db.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(value), 'checkin:' + sid))
        return value

    async def wait(predicate):
        await async_wait_until(predicate, timeout=40)

    async def initial(desc):
        nonlocal sid
        async with websocket(home, desc) as ws:
            reply = await rpc(ws, 'session.create', request_id='local-supervisor', source='gui',
                              cwd=str(home), toolsets=[])
            assert 'result' in reply, reply
            sid = reply['result']['session_id']
            reply = await rpc(ws, 'prompt.submit', session_id=sid, input_id='warm', text='WARM_AUTOMATION')
            assert 'result' in reply, reply
            await wait(lambda: rows()[0]['status'] == 'terminal')
            async def goal(call, *args, **kwargs):
                reply = await rpc(ws, 'session.automation', session_id=sid,
                                  kind='goal', call=call, args=list(args), kwargs=kwargs)
                assert 'result' in reply, reply
                return reply['result']
            result = await goal('set', 'Verify the local peer artifact')
            instance = json.loads(result['state'])['created_at']
            await goal('wait_for_seconds', 3600, reason='await the peer artifact')
            cli = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'hermes_cli.goal_peer_command', 'wait', str(instance),
                'hermes:default/local-peer', 'local-peer-work', 'artifact readback',
                cwd=root, env={**env, 'HERMES_SESSION_ID': sid},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(cli.communicate(), 30)
                assert cli.returncode == 0, stdout.decode() + stderr.decode()
                assert json.loads(stdout)['value']['ok'], stdout
            finally:
                if cli.returncode is None:
                    cli.kill(); await cli.wait()
            await wait(lambda: state() is not None)
            armed = advance(due_at=time.time() - 1)
            await wait(lambda: len(ticks()) == 1)
            row = ticks()[0]
            assert row['status'] == 'queued', row
            payload = json.loads(row['payload_json'])
            descriptor = payload['local_automation_v1']
            assert descriptor['checkin'] == [sid, armed['due_at']], descriptor
            assert descriptor['display_kind'] == 'supervisor_checkin', descriptor
            assert descriptor['display_text'] == 'Supervisor check-in', descriptor
            assert descriptor['owner'] == sid and descriptor['target'] == sid, descriptor
            assert model.checkin_turns == 0
            return armed, row

    async def started(desc, armed, committed):
        # Startup restores the persisted LOCAL descriptor and runs the queued head.
        assert await asyncio.to_thread(model.checkin_blocked.wait, 35)
        row = ticks()[0]
        assert row['admission_id'] == committed['admission_id'] and row['status'] == 'started', row
        observed = state()
        assert observed['due_at'] == armed['due_at'], observed
        assert observed['last_visible_at'] == armed['last_visible_at'], observed
        assert observed['next_attempt_at'] > time.time() + 1700, observed
        assert model.checkin_turns == 1
        return observed

    async def recovered(desc, before_crash):
        await wait(lambda: ticks()[0]['status'] == 'terminal')
        assert ticks()[0]['outcome'] == 'interrupted', ticks()
        assert model.checkin_turns == 1, model.requests
        assert state()['due_at'] == before_crash['due_at'], state()
        assert state()['last_visible_at'] == before_crash['last_visible_at'], state()
        assert state()['next_attempt_at'] == before_crash['next_attempt_at'], state()
        # No model replay of a claimed execution. A new bounded attempt is a new admission.
        async with websocket(home, desc) as ws:
            reply = await rpc(ws, 'session.resume', session_id=sid)
            assert 'result' in reply, reply
            advance(next_attempt_at=time.time() - 1)
            await wait(lambda: len(ticks()) == 2 and ticks()[-1]['status'] == 'terminal')
            await wait(lambda: state()['due_at'] > time.time() + 1700)
            reply = await rpc(ws, 'session.resume', session_id=sid)
            assert 'result' in reply, reply
            history = reply['result']['messages']
            assert 'SUPERVISOR_STATUS' in json.dumps(history), history
            cards = [m for m in history if m.get('display_kind') == 'supervisor_checkin']
            assert cards and all(m.get('role') == 'user' for m in cards), cards
            assert model.checkin_turns == 2, model.requests
            retries = [request for request in model.requests
                       if any('required supervisor status' in str(message.get('content', ''))
                              for message in request['messages']
                              if message['role'] in ('system', 'developer'))]
            assert len(retries) == 1, retries
            return state(), history

    async def replay(desc, delivered, history):
        async with websocket(home, desc) as ws:
            reply = await rpc(ws, 'session.resume', session_id=sid)
            assert 'result' in reply, reply
            assert reply['result']['messages'] == history, reply
            assert state()['due_at'] == delivered['due_at'], state()
            assert state()['last_visible_at'] == delivered['last_visible_at'], state()
            assert len(ticks()) == 2 and all(r['status'] == 'terminal' for r in ticks()), ticks()
            assert model.checkin_turns == 2, model.requests
            assert reply['result']['info']['source'] == 'gui', reply

    def boot(barrier):
        return daemon(root, home, env, barrier=barrier, fixture='local_automation_daemon.py')

    try:
        with boot(True) as (proc, desc):
            pids.append(proc.pid); epochs.append(desc['authority_epoch'])
            armed, committed = asyncio.run(initial(desc))
        with boot(False) as (proc, desc):
            pids.append(proc.pid); epochs.append(desc['authority_epoch'])
            before_crash = asyncio.run(started(desc, armed, committed))
        model.checkin_release.set()
        with boot(False) as (proc, desc):
            pids.append(proc.pid); epochs.append(desc['authority_epoch'])
            delivered, history = asyncio.run(recovered(desc, before_crash))
        with boot(False) as (proc, desc):
            pids.append(proc.pid); epochs.append(desc['authority_epoch'])
            asyncio.run(replay(desc, delivered, history))
        assert len(set(pids)) == 4 and epochs == sorted(set(epochs)), (pids, epochs)
        print(json.dumps({'pids': pids, 'epochs': epochs, 'ledger': ticks(),
                          'queued_restore_executed_once': True, 'started_crash_not_replayed': True,
                          'visible_due_preserved_until_delivery': True, 'durable_status_replayed': True,
                          'model_checkin_turns': model.checkin_turns, 'local_source': 'gui'}))
    except BaseException:
        for path in (home / 'first.log', home / 'restart.log'):
            if path.exists():
                print(path.read_text()[-15000:], file=sys.stderr)
        raise
    finally:
        model.checkin_release.set(); model.release.set()
        model.shutdown(); model.server_close()


if __name__ == '__main__':
    probe(Path(sys.argv[1]))
