"""Ordinary daemon, real terminal child, loopback-only relay/provider peers."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from typing import Any, cast
import shlex
import sqlite3
import subprocess
import sys
import threading
import time

import websockets
from tests.gateway.test_normal_runtime_boot import control
from tests.waiting import async_wait_until
from tests.gateway.fixtures.local_recovery_probe import websocket, rpc


class AutomationServer(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    blocked: threading.Event
    release: threading.Event
    hold_completion: bool
    completion_blocked: threading.Event
    completion_release: threading.Event
    checkin: bool
    checkin_turns: int
    checkin_release: threading.Event
    checkin_blocked: threading.Event
    status_text: str
    gate: Path


class Model(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self):
        server = cast(AutomationServer, self.server)
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = body.get('messages', [])
        if messages:
            server.requests.append(body)
        last = next((m.get('content', '') for m in reversed(messages) if m['role'] == 'user'), '')
        if 'HOLD_AUTOMATION' in str(last):
            server.blocked.set()
            server.release.wait(120)
        if (getattr(server, 'hold_completion', False)
                and 'REAL_TERMINAL_COMPLETION' in str(last)):
            server.completion_blocked.set()
            server.completion_release.wait(120)
        message: dict[str, Any] = {'role': 'assistant', 'content': 'AUTOMATION_ACK'}
        if getattr(server, 'checkin', False):
            if '[Supervisor check-in]' in str(last):
                server.checkin_turns += 1
                if hasattr(server, 'checkin_release'):
                    server.checkin_blocked.set()
                    server.checkin_release.wait(60)
                message['content'] = 'NO_REPLY'
            # Recovery guidance is API-only system text, not a new human row.
            elif any('required supervisor status' in str(m.get('content', ''))
                     for m in messages if m['role'] in ('system', 'developer')):
                message['content'] = getattr(server, 'status_text', 'SUPERVISOR_STATUS: tests are running. Next check in 30 minutes.')
            elif 'REAL_TERMINAL_COMPLETION' in str(last):
                message['content'] = 'COMPLETION_STATUS: background work finished.'
        if 'SPAWN_AUTOMATION' in str(last) and messages[-1]['role'] != 'tool':
            code = ('import pathlib,time; p=pathlib.Path(' + repr(str(server.gate)) + '); '
                    '\nwhile not p.exists(): time.sleep(.02)\nprint("REAL_TERMINAL_COMPLETION",flush=True)')
            command = shlex.join([sys.executable, '-c', code])
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'terminal-spawn',
                'type': 'function', 'function': {'name': 'terminal', 'arguments': json.dumps({
                    'command': command, 'background': True, 'notify_on_complete': not getattr(self.server, 'watch', False),
                    **({'watch_patterns': ['REAL_TERMINAL_COMPLETION']} if getattr(self.server, 'watch', False) else {})})}}]}
        finish = 'tool_calls' if message.get('tool_calls') else 'stop'
        payload = json.dumps({'id': 'local', 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}).encode()
        kind = 'application/json'
        if body.get('stream'):
            if message.get('tool_calls'):
                cast(dict[str, Any], message['tool_calls'][0])['index'] = 0
            payload = ('data: ' + json.dumps({'id': 'local', 'choices': [
                {'index': 0, 'delta': message, 'finish_reason': finish}]}) + '\n\ndata: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


async def _identify_until_ready(home, seconds):
    """Last ``identify`` descriptor once the daemon reports ready, or when ``seconds`` elapse."""
    desc = {}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            desc = await asyncio.to_thread(control, home, 'identify')
            if desc.get('state') == 'ready':
                break
        except (OSError, ValueError):
            pass
        await asyncio.sleep(.05)
    return desc


async def probe(root, base, mode):
    home, user = base / 'state', base / 'user'
    home.mkdir(); user.mkdir()
    model = AutomationServer(('127.0.0.1', 0), Model)
    model.requests = []
    model.blocked, model.release = threading.Event(), threading.Event()
    model.hold_completion = mode.startswith('restart-')
    model.completion_blocked, model.completion_release = threading.Event(), threading.Event()
    model.gate = home / 'release-terminal'
    model.checkin = mode in ('checkin', 'peer-checkin', 'checkin-race-admission', 'checkin-race-execution')
    race = mode.startswith('checkin-race-')
    spawn_terminal = mode in {'terminal', 'checkin'} or race or mode.startswith('restart-')
    if mode == 'peer-checkin':
        model.status_text = 'SUPERVISOR_STATUS: peer result still awaits parent verification. Next check in 30 minutes.'
    model.checkin_turns = 0
    threading.Thread(target=model.serve_forever, daemon=True).start()
    ready = asyncio.Queue()
    outgoing = []
    outbound_at = {}
    status_ack = asyncio.Event()
    async def connector(ws):
        hello = json.loads(await ws.recv())
        assert hello['platform'] == 'telegram', hello
        descriptor = {'contract_version': 1, 'platform': 'telegram', 'label': 'Fixture',
            'max_message_length': 4096, 'supports_draft_streaming': False, 'supports_edit': True,
            'supports_threads': model.checkin, 'markdown_dialect': 'plain', 'len_unit': 'chars'}
        await ws.send(json.dumps({'type': 'descriptor', 'descriptor': descriptor}) + '\n')
        await ready.put(ws)
        async for line in ws:
            frame = json.loads(line)
            outgoing.append(frame)
            if frame['type'] == 'outbound':
                if 'AUTOMATION_ACK' in json.dumps(frame):
                    outbound_at['initial'] = time.time()
                if 'SUPERVISOR_STATUS' in json.dumps(frame):
                    outbound_at['supervisor'] = time.time()
                    await status_ack.wait()
                await ws.send(json.dumps({'type': 'outbound_result', 'requestId': frame['requestId'],
                    'result': {'success': True, 'message_id': 'fixture-final'}}) + '\n')
            elif frame['type'] == 'going_idle':
                await ws.send(json.dumps({'type': 'going_idle_ack'}) + '\n')

    def rows():
        with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM session_admissions ORDER BY seq')]

    async def wait_for(predicate, seconds=35):
        await async_wait_until(predicate, timeout=seconds)

    proc = None
    try:
        async with websockets.serve(connector, '127.0.0.1', 0) as relay:
            url = f'http://127.0.0.1:{model.server_port}/v1'
            cfg = {'gateway': {'multiplex_profiles': False, 'completion_notification_batch_window_seconds': 0,
                               'restart_resume_policy': 'continue'},
                'model': {'provider': 'custom', 'default': 'local-fixture', 'base_url': url},
                'streaming': {'enabled': False}, 'terminal': {'cwd': str(home)},
                'approvals': {'mode': 'off'},
                'auxiliary': {'title_generation': {'enabled': False}}}
            (home / 'config.yaml').write_text(json.dumps(cfg))
            env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'TIRITH_ENABLED') if k in os.environ}
            env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
                OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1',
                HERMES_TEST_ISOLATION=str(home), SUPERVISOR_BOUNDARY=mode,
                GATEWAY_RELAY_URL=f'ws://127.0.0.1:{next(iter(relay.sockets)).getsockname()[1]}/relay',
                GATEWAY_RELAY_PLATFORMS='telegram', GATEWAY_RELAY_BOT_IDS='{"telegram":{"botId":"fixture-bot"}}',
                GATEWAY_RELAY_ID='fixture-gateway', GATEWAY_RELAY_SECRET='fixture-secret')
            with (home / 'daemon.log').open('w+') as log:
                launch = ([sys.executable, str(root / 'tests/gateway/fixtures/supervisor_boundary_daemon.py')]
                          if race else [sys.executable, '-m', 'gateway.run'])
                proc = subprocess.Popen(launch, cwd=root, env=env,  # noqa: ASYNC220 -- Popen returns immediately; the test drives the child through its handle
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                try:
                    ws = await asyncio.wait_for(ready.get(), 35)
                    desc = await _identify_until_ready(home, 35)
                    assert desc.get('state') == 'ready', desc
                    async def send(identity, text):
                        await ws.send(json.dumps({'type': 'inbound', 'event': {'text': text,
                            'message_id': identity, 'source': {'platform': 'telegram', 'chat_id': 'automation-chat',
                            'chat_type': 'dm', 'user_id': 'fixture-user',
                            **({'thread_id': '103'} if model.checkin else {})}}}) + '\n')
                    await send('spawn', 'SPAWN_AUTOMATION' if spawn_terminal else 'WARM_AUTOMATION')
                    await wait_for(lambda: any(r['request_id'] == 'spawn' and r['status'] == 'terminal' for r in rows()))
                    if spawn_terminal:
                        assert 'REAL_TERMINAL_COMPLETION' in json.dumps(model.requests), 'model did not request real terminal'
                    elif mode == 'heartbeat':
                        await send('timer-config', '/heartbeat every 1m TIMER_AUTOMATION')
                        await wait_for(lambda: 'Heartbeat' in json.dumps(outgoing))
                    sid = rows()[0]['target_session_id']
                    if mode == 'peer-checkin':
                        async with websocket(home, desc) as parent:
                            await rpc(parent, 'session.resume', session_id=sid)
                            async def goal(call, *args, **kwargs):
                                reply = await rpc(parent, 'session.automation', session_id=sid,
                                                  kind='goal', call=call, args=list(args), kwargs=kwargs)
                                assert 'result' in reply, reply
                                return reply['result']
                            result = await goal('set', 'Verify the peer artifact')
                            instance = json.loads(result['state'])['created_at']
                            await goal('wait_for_seconds', 3600, reason='await the peer artifact')
                            cli = await asyncio.create_subprocess_exec(
                                sys.executable, '-m', 'hermes_cli.goal_peer_command', 'wait', str(instance),
                                'hermes:default/idle-peer', 'existing-peer-work', 'artifact readback',
                                cwd=root, env={**env, 'HERMES_SESSION_ID': sid},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                            try:
                                stdout, stderr = await asyncio.wait_for(cli.communicate(), 30)
                                assert cli.returncode == 0, stdout.decode() + stderr.decode()
                                assert json.loads(stdout)['value']['ok'], stdout
                            finally:
                                if cli.returncode is None:
                                    cli.kill()
                                    await cli.wait()
                    if model.checkin:
                        def state():
                            with sqlite3.connect(home / 'state.db') as db:
                                row = db.execute('SELECT value FROM state_meta WHERE key=?', ('checkin:' + sid,)).fetchone()
                                return json.loads(row[0]) if row and row[0] else None
                        # Native execution settles before transport final delivery. Wait for
                        # the acknowledged initial reply's timer reset before advancing it.
                        await wait_for(lambda: state() is not None and 'initial' in outbound_at
                                       and state()['last_visible_at'] >= outbound_at['initial'])
                        armed = state()
                        # Advance only this test timer, not the daemon's clock or production config.
                        armed['due_at'] = time.time() - 1
                        with sqlite3.connect(home / 'state.db') as db:
                            db.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(armed), 'checkin:' + sid))
                        if race:
                            await wait_for((home / 'boundary-ready').exists)
                            await send('human-race', 'HUMAN_FIFO_FOLLOWER')
                            await wait_for((home / 'human-seen').exists)
                            if mode == 'checkin-race-execution':
                                await wait_for(lambda: any(r['request_id'] == 'human-race' for r in rows()))
                            (home / 'boundary-release').touch()
                            await wait_for(lambda: any(r['request_id'] == 'human-race' and r['status'] == 'terminal'
                                                      for r in rows()))
                            automatic = [r for r in rows() if 'supervisor-checkin' in r['request_id']]
                            assert len(automatic) == 1 and automatic[0]['status'] == 'terminal', automatic
                            assert model.checkin_turns == 0, model.requests
                            # The native finite owner settles a no-response discard as completed.
                            # Expendability means no model/send and no FIFO pause, not a new outcome.
                            assert automatic[0]['outcome'] == 'completed', automatic
                            assert not any('SUPERVISOR_STATUS' in json.dumps(f) for f in outgoing), outgoing
                            await send('after-race', 'AFTER_RACE')
                            await wait_for(lambda: any(r['request_id'] == 'after-race' and r['status'] == 'terminal'
                                                      for r in rows()))
                            print(json.dumps({'boundary': mode, 'ledger': rows(), 'model_checkin_turns': 0,
                                              'human_follower_drained': True, 'successor_drained': True}))
                            return
                        try:
                            await wait_for(lambda: 'SUPERVISOR_STATUS' in json.dumps(outgoing))
                        except AssertionError as exc:
                            raise AssertionError(json.dumps({
                                'outgoing': outgoing,
                                'model_inputs': [next((m.get('content', '') for m in reversed(r['messages'])
                                                       if m['role'] == 'user'), '')[:500]
                                                 for r in model.requests],
                                'admissions': [{k: r[k] for k in ('request_id', 'status', 'outcome')} for r in rows()],
                            })) from exc
                        assert model.checkin_turns == 1, model.requests
                        automatic = [r for r in rows() if 'supervisor-checkin' in r['request_id']]
                        assert len(automatic) == 1 and automatic[0]['status'] == 'terminal', automatic
                        # An outbound frame is not a visible ACK. Hold the real relay response
                        # until the daemon proves execution did not erase its overdue debt.
                        before_ack = state()
                        assert before_ack is not None and armed is not None
                        assert before_ack['fired'] and before_ack['due_at'] == armed['due_at'], before_ack
                        assert before_ack['last_visible_at'] == armed['last_visible_at'], before_ack
                        assert before_ack['next_attempt_at'] > time.time() + 1700, before_ack
                        status_ack.set()
                        await wait_for(lambda: state()['last_visible_at'] >= outbound_at['supervisor'])
                        assert state()['due_at'] > time.time() + 1700
                        status_frame = next(f for f in outgoing if 'SUPERVISOR_STATUS' in json.dumps(f))
                        assert status_frame['action']['chat_id'] == 'automation-chat', status_frame
                        assert str(status_frame['action']['metadata']['thread_id']) == '103', status_frame
                        assert status_frame['platform'] == 'telegram' and status_frame['botId'] == 'fixture-bot', status_frame
                        retries = [r for r in model.requests if any('required supervisor status' in str(m.get('content', ''))
                                   for m in r['messages'] if m['role'] in ('system', 'developer'))]
                        assert len(retries) == 1, retries
                        if mode == 'peer-checkin':
                            async with websocket(home, desc) as parent:
                                await rpc(parent, 'session.resume', session_id=sid)
                                unchanged = await goal('state')
                                assert len(json.loads(unchanged['state'])['pending_peers']) == 1
                                refused = await goal('resolve_peer', 'hermes:default/idle-peer', 'existing-peer-work',
                                                     'pytest artifact: 1 passed', expected_instance=instance)
                                assert not refused['value']['ok'], refused
                                assert state() is not None
                                await goal('clear')
                            await wait_for(lambda: state() is None)
                            print(json.dumps({'peer_only': True, 'unverified_idle_still_pending': True,
                                'supervisor_turns': model.checkin_turns, 'silence_recovered': True,
                                'explicit_user_clear_disarmed': True, 'topic': '103'}))
                            return
                        model.gate.touch()
                        await wait_for(lambda: 'COMPLETION_STATUS' in json.dumps(outgoing))
                        await wait_for(lambda: state() is None)
                        assert model.checkin_turns == 1
                        print(json.dumps({'supervisor_turns': model.checkin_turns, 'silence_recovered': True,
                                          'completion_disarmed': True, 'topic': '103'}))
                        return
                    async with websocket(home, desc) as observer:
                        assert 'result' in await rpc(observer, 'session.resume', session_id=sid)
                    if mode.startswith('restart-'):
                        model.gate.touch()
                        assert await asyncio.to_thread(model.completion_blocked.wait, 20)
                        automatic = [r for r in rows() if r['principal_id'].startswith('automation:')]
                        assert len(automatic) == 1 and automatic[0]['status'] == 'started', rows()
                        await send('human-follower', 'HUMAN_FIFO_FOLLOWER')
                        await wait_for(lambda: any(r['request_id'] == 'human-follower' for r in rows()))
                        old_pid, old_epoch = proc.pid, desc['authority_epoch']
                        sent_before_restart = len(outgoing)
                        proc.kill()
                        await asyncio.to_thread(proc.wait, 10)
                        model.hold_completion = False
                        model.completion_release.set()
                        if mode == 'restart-denied':
                            env['GATEWAY_RELAY_SECRET'] = 'changed-fixture-secret'
                        proc = await asyncio.to_thread(subprocess.Popen,
                            [sys.executable, '-m', 'gateway.run'], cwd=root, env=env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                        ws = await asyncio.wait_for(ready.get(), 35)
                        desc = await _identify_until_ready(home, 35)
                        assert desc.get('state') == 'ready', desc
                        assert proc.pid != old_pid and desc['authority_epoch'] > old_epoch
                        if mode == 'restart-denied':
                            ledger = rows()
                            recovered = [r for r in ledger if r['principal_id'].startswith('automation:')]
                            assert len(recovered) == 1 and recovered[0]['outcome'] == 'interrupted', ledger
                            assert recovered[0]['admission_id'] == automatic[0]['admission_id']
                            assert next(r for r in ledger if r['request_id'] == 'human-follower')['status'] == 'queued'
                            verdicts = [line for line in (home / 'logs/gateway.log').read_text().splitlines()
                                if 'Native session startup recovery' in line and sid in line]
                            assert any('profile_mismatch' in line for line in verdicts), verdicts
                            texts = [next((m.get('content', '') for m in reversed(r['messages'])
                                if m['role'] == 'user'), '') for r in model.requests if r.get('messages')]
                            assert sum('REAL_TERMINAL_COMPLETION' in str(t) for t in texts) == 1, texts
                            assert not any('HUMAN_FIFO_FOLLOWER' in str(t) for t in texts), texts
                            print(json.dumps({'ledger': ledger, 'model_inputs': texts, 'recovery_verdicts': verdicts}))
                            return
                        await wait_for(lambda: all(r['status'] == 'terminal' for r in rows()))
                        recovered = [r for r in rows() if r['principal_id'].startswith('automation:')]
                        assert len(recovered) == 1 and recovered[0]['outcome'] == 'interrupted', rows()
                        assert recovered[0]['admission_id'] == automatic[0]['admission_id']
                        await wait_for(lambda: len(outgoing) > sent_before_restart)
                        revived_sends = [f['action'] for f in outgoing[sent_before_restart:]
                            if f.get('type') == 'outbound' and f.get('action', {}).get('op') == 'send'
                            and f['action'].get('content') == 'AUTOMATION_ACK']
                        assert len(revived_sends) == 1, outgoing[sent_before_restart:]
                        assert revived_sends[0].get('metadata', {}).get('user_id') == 'fixture-user', revived_sends
                        await send('after-recovery', 'AFTER_RECOVERY')
                        await wait_for(lambda: any(r['request_id'] == 'after-recovery'
                            and r['status'] == 'terminal' for r in rows()))
                        texts = [next((m.get('content', '') for m in reversed(r['messages'])
                            if m['role'] == 'user'), '') for r in model.requests if r.get('messages')]
                        assert sum('REAL_TERMINAL_COMPLETION' in str(t) for t in texts) == 1, texts
                        assert sum('HUMAN_FIFO_FOLLOWER' in str(t) for t in texts) == 1, texts
                        assert sum('AFTER_RECOVERY' in str(t) for t in texts) == 1, texts
                        with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as db:
                            notices = db.execute("SELECT content FROM messages WHERE session_id=? AND role='assistant'",
                                                 (sid,)).fetchall()
                        assert any('interrupt' in str(n).lower() for n in notices), notices
                        print(json.dumps({'old_pid': old_pid, 'new_pid': proc.pid, 'ledger': rows(),
                                          'model_inputs': texts, 'interruption_notices': notices}))
                        return
                    await send('held', 'HOLD_AUTOMATION')
                    assert await asyncio.to_thread(model.blocked.wait, 15)
                    await send('human-follower', 'HUMAN_FIFO_FOLLOWER')
                    await wait_for(lambda: any(r['request_id'] == 'human-follower' for r in rows()))
                    model.gate.touch()
                    # Real cadence: process polls every five seconds; heartbeat interval is one minute.
                    # Poll for the producer's commit instead of sleeping out the whole bound.
                    deadline = time.monotonic() + (8 if mode == 'terminal' else 70)
                    while (time.monotonic() < deadline
                           and not any(r['principal_id'].startswith('automation:') for r in rows())):
                        await asyncio.sleep(.1)
                    ledger = rows()
                    automatic = [r for r in ledger if r['principal_id'].startswith('automation:')]
                    assert automatic, {'failure': 'producer ACK bypassed canonical durable FIFO', 'ledger': ledger,
                                       'daemon_tail': (home / 'daemon.log').read_text()[-6000:]}
                    assert automatic[0]['status'] == 'queued', automatic
                    assert next(r for r in ledger if r['request_id'] == 'held')['status'] == 'started'
                    assert automatic[0]['seq'] > next(r for r in ledger if r['request_id'] == 'human-follower')['seq']
                    model.release.set()
                    await wait_for(lambda: all(r['status'] == 'terminal' for r in rows()))
                    assert all(r['outcome'] == 'completed' for r in rows()), rows()
                    texts = [next((m.get('content', '') for m in reversed(r['messages']) if m['role'] == 'user'), '')
                             for r in model.requests if r.get('messages')]
                    marker = 'REAL_TERMINAL_COMPLETION' if mode == 'terminal' else 'TIMER_AUTOMATION'
                    assert marker in str(texts[-1]), texts
                    assert 'HUMAN_FIFO_FOLLOWER' in str(texts[-2]), texts
                    print(json.dumps({'daemon_pid': proc.pid, 'ledger': rows(), 'model_inputs': texts,
                                      'observer_closed_before_completion': True}))
                except BaseException:
                    (home / 'boundary-release').touch()
                    status_ack.set()
                    if (home / 'state.db').exists():
                        print(json.dumps({'ledger_at_failure': rows(), 'outgoing': outgoing,
                            'model_inputs': [next((m.get('content', '') for m in reversed(r['messages'])
                                if m['role'] == 'user'), '') for r in model.requests if r.get('messages')]}),
                            file=sys.stderr)
                    print((home / 'daemon.log').read_text()[-24000:], file=sys.stderr)
                    for name in ('logs/gateway.log', 'sessions/sessions.json', 'sessions.json'):
                        diagnostic = home / name
                        if diagnostic.is_file():
                            print(name + '\n' + diagnostic.read_text()[-24000:], file=sys.stderr)
                    raise
    finally:
        model.gate.touch()
        model.release.set()
        model.completion_release.set()
        if proc is not None:
            proc.kill(); proc.wait(timeout=10)
        model.shutdown(); model.server_close()


if __name__ == '__main__':
    asyncio.run(probe(Path(__file__).resolve().parents[3], Path(sys.argv[1]), sys.argv[2]))
