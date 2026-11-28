"""Ordinary daemon, real terminal child, loopback-only relay/provider peers."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import functools
import json
import os
from pathlib import Path
from typing import Any, cast
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import websockets
from websockets.exceptions import ConnectionClosedOK

from tests.gateway.test_normal_runtime_boot import control
from tests.waiting import async_wait_until
from tests.gateway.fixtures.local_recovery_probe import child_env, websocket, rpc


_AUTOMATION_ACK = 'AUTOMATION_ACK'
_FOLLOWER_ACK = 'FIFO_FOLLOWER_ACK'
_AFTER_RECOVERY_ACK = 'AFTER_RECOVERY_ACK'


class AutomationServer(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    blocked: threading.Event
    release: threading.Event
    hold_completion: bool
    recovery_replies: bool
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
        message: dict[str, Any] = {'role': 'assistant', 'content': _AUTOMATION_ACK}
        if getattr(server, 'recovery_replies', False):
            if 'HUMAN_FIFO_FOLLOWER' in str(last):
                message['content'] = _FOLLOWER_ACK
            elif 'AFTER_RECOVERY' in str(last):
                message['content'] = _AFTER_RECOVERY_ACK
        if getattr(server, 'checkin', False):
            if '[Supervisor check-in]' in str(last):
                server.checkin_turns += 1
                if getattr(server, 'checkin_failure', False):
                    payload = json.dumps({'error': {'message': 'Fixture supervisor request rejected',
                        'type': 'invalid_request_error', 'code': 'invalid_request_error'}}).encode()
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if hasattr(server, 'checkin_release'):
                    server.checkin_blocked.set()
                    server.checkin_release.wait(60)
                message['content'] = ('NO_REPLY' if getattr(server, 'checkin_silent', False) else
                    getattr(server, 'status_text', 'SUPERVISOR_STATUS: tests are running.'))
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


def _model_for(home, mode):
    """The fake model server for ``mode``, plus the mode's derived flags."""
    model = AutomationServer(('127.0.0.1', 0), Model)
    model.requests = []
    model.blocked, model.release = threading.Event(), threading.Event()
    model.hold_completion = mode.startswith('restart-')
    model.recovery_replies = mode == 'restart-started'
    model.completion_blocked, model.completion_release = threading.Event(), threading.Event()
    model.gate = home / 'release-terminal'
    peer_checkin = mode.startswith('peer-checkin')
    model.checkin = peer_checkin or mode in ('checkin', 'checkin-race-admission', 'checkin-race-execution')
    model.checkin_failure = mode == 'peer-checkin-failure'
    if peer_checkin:
        model.status_text = 'SUPERVISOR_STATUS: peer result still awaits parent verification. Next check in 30 minutes.'
    model.checkin_turns = 0
    race = mode.startswith('checkin-race-')
    spawn_terminal = mode in {'terminal', 'checkin'} or race or mode.startswith('restart-')
    status_marker = 'Still outstanding:' if model.checkin_failure else 'SUPERVISOR_STATUS'
    return model, peer_checkin, race, spawn_terminal, status_marker


async def _goal_call(parent, sid, call, *args, **kwargs):
    reply = await rpc(parent, 'session.automation', session_id=sid,
                      kind='goal', call=call, args=list(args), kwargs=kwargs)
    assert 'result' in reply, reply
    return reply['result']


async def _arm_peer_goal_wait(root, env, home, desc, sid):
    """Set a goal on ``sid`` and park it on a peer's work through the real goal_peer_command CLI.
    Returns the goal instance."""
    async with websocket(home, desc) as parent:
        await rpc(parent, 'session.resume', session_id=sid)
        goal = functools.partial(_goal_call, parent, sid)
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
    return instance


def _owner_is_running(pid):
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    try:
        if hasattr(os, 'waitid'):
            return os.waitid(os.P_PID, pid, flags) is None
        if sys.platform != 'darwin':
            raise RuntimeError('Owner fixture requires non-reaping waitid')

        # Older macOS Python builds omit os.waitid, but Darwin's libc exposes it.
        import ctypes as c

        class Siginfo(c.Structure):
            # Darwin siginfo_t (sys/signal.h); only si_pid is read.
            _fields_ = [('si_signo', c.c_int), ('si_errno', c.c_int), ('si_code', c.c_int),
                        ('si_pid', c.c_int), ('si_uid', c.c_uint), ('si_status', c.c_int),
                        ('si_addr', c.c_void_p), ('si_value', c.c_void_p),
                        ('si_band', c.c_long), ('pad', c.c_ulong * 7)]

        waitid = c.CDLL(None, use_errno=True).waitid
        waitid.argtypes = (c.c_int, c.c_uint, c.POINTER(Siginfo), c.c_int)
        waitid.restype = c.c_int
        info = Siginfo()
        if waitid(os.P_PID, pid, c.byref(info), flags) == -1:
            errno = c.get_errno()
            raise OSError(errno, os.strerror(errno))
        return info.si_pid == 0
    except ChildProcessError:
        return False  # The watcher already reaped it.


def _signal_owner(proc):
    if proc.returncode is not None:
        return
    try:
        if os.name == 'nt':
            proc.kill()  # TerminateProcess does not compete for waitpid.
        elif _owner_is_running(proc.pid):
            # Process.kill() on Python 3.11 can reap via Popen.poll(). WNOWAIT leaves
            # that to asyncio. A small check-to-kill race with exit/reaping remains.
            os.kill(proc.pid, signal.SIGKILL)  # windows-footgun: ok
    except ProcessLookupError:
        pass  # The watcher won the race with kill; still use its status.


async def _kill_and_reap(proc, phase):
    _signal_owner(proc)
    try:
        return await asyncio.wait_for(proc.wait(), 10)
    except TimeoutError as exc:
        raise TimeoutError(f'{phase}: PID {proc.pid} exit not reaped within 10s of kill') from exc


class ProbeBudget:
    def __init__(self, deadline):
        self.deadline = deadline
        self.completed = []

    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    @asynccontextmanager
    async def phase(self, name):
        started = time.monotonic()
        overall = asyncio.timeout(self.remaining())
        try:
            async with overall:
                yield
        except TimeoutError as exc:
            if not overall.expired():
                raise  # Preserve an earlier phase-local timeout and its diagnostic.
            raise self.exhausted(name, started) from exc
        if self.remaining() == 0:
            raise self.exhausted(name, started)
        self.completed.append((name, time.monotonic() - started))

    def exhausted(self, name, started):
        completed = ', '.join(f'{phase}={elapsed:.3f}s' for phase, elapsed in self.completed)
        message = (f'{name}: overall budget exhausted after {time.monotonic() - started:.3f}s '
                   f'in this phase; completed phases: {completed or "none"}')
        print(message, file=sys.stderr, flush=True)  # Before diagnostics and best-effort teardown.
        return TimeoutError(message)

    async def wait(self, name, awaitable):
        async with self.phase(name):
            return await awaitable


async def probe(root, base, mode, *, deadline):
    budget = ProbeBudget(deadline)
    home, user = base / 'state', base / 'user'
    home.mkdir(); user.mkdir()
    model, peer_checkin, race, spawn_terminal, status_marker = _model_for(home, mode)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    ready = asyncio.Queue()
    outgoing = []
    outbound_at = {}
    status_ack = asyncio.Event()
    old_ws = None
    reply_sources = {}
    delayed_replies = []
    expose_successor_reply = False
    successor_ws = None
    successor_idle_ack = asyncio.Event()
    successor_relay_drained = asyncio.Event()
    async def connector(ws):
        hello = json.loads(await ws.recv())
        assert hello['platform'] == 'telegram', hello
        descriptor = {'contract_version': 1, 'platform': 'telegram', 'label': 'Fixture',
            'max_message_length': 4096, 'supports_draft_streaming': False, 'supports_edit': True,
            'supports_threads': model.checkin, 'markdown_dialect': 'plain', 'len_unit': 'chars'}
        await ws.send(json.dumps({'type': 'descriptor', 'descriptor': descriptor}) + '\n')
        await ready.put(ws)
        can_ack = True
        async for line in ws:
            frame = json.loads(line)
            action = frame.get('action', {})
            if (frame['type'] == 'outbound' and action.get('op') == 'send'
                    and action.get('content') in (_FOLLOWER_ACK, _AFTER_RECOVERY_ACK)):
                reply_sources[id(action)] = ws
                if (action['content'] == _FOLLOWER_ACK and old_ws is not None and ws is not old_ws
                        and not expose_successor_reply):
                    # Hold only observation, not the wire acknowledgement or durable settlement.
                    delayed_replies.append(frame)
                else:
                    outgoing.append(frame)
            else:
                outgoing.append(frame)
            ack = None
            if frame['type'] == 'outbound':
                serialized = json.dumps(frame)
                if _AUTOMATION_ACK in serialized:
                    outbound_at['initial'] = time.time()
                if status_marker in serialized:
                    outbound_at['supervisor'] = time.time()
                    if can_ack:
                        await status_ack.wait()
                ack = {'type': 'outbound_result', 'requestId': frame['requestId'],
                       'result': {'success': True, 'message_id': 'fixture-final'}}
            elif frame['type'] == 'going_idle':
                ack = {'type': 'going_idle_ack'}
            if can_ack and ack is not None:
                try:
                    await ws.send(json.dumps(ack) + '\n')
                except ConnectionClosedOK:
                    # The peer can finish closing before we ACK already buffered frames.
                    # Keep observing them; only the write side is finished.
                    can_ack = False
                else:
                    if frame['type'] == 'going_idle' and ws is successor_ws:
                        successor_idle_ack.set()
        if ws is successor_ws:
            successor_relay_drained.set()

    def rows():
        with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM session_admissions ORDER BY seq')]

    async def wait_for(phase, predicate, seconds=35):
        await budget.wait(phase, async_wait_until(predicate, timeout=seconds, what=phase))

    async def wait_for_model(phase, event, seconds):
        # Don't leave the executor waiting out the full phase after overall expiry.
        async with budget.phase(phase):
            return await asyncio.to_thread(event.wait, min(seconds, budget.remaining()))

    proc = old_proc = None
    old_pid = None
    try:
        async with AsyncExitStack() as relay_stack:
            relay = await budget.wait('relay startup', relay_stack.enter_async_context(
                websockets.serve(connector, '127.0.0.1', 0)))
            url = f'http://127.0.0.1:{model.server_port}/v1'
            cfg = {'gateway': {'multiplex_profiles': False, 'completion_notification_batch_window_seconds': 0,
                               'restart_resume_policy': 'continue'},
                'model': {'provider': 'custom', 'default': 'local-fixture', 'base_url': url},
                'streaming': {'enabled': False}, 'terminal': {'cwd': str(home)},
                # The follower must queue behind the held turn: under the default ``interrupt`` a relay
                # follow-up redirects the running turn instead of taking its own FIFO row.
                'display': {'busy_input_mode': 'queue'},
                'approvals': {'mode': 'off'},
                'auxiliary': {'title_generation': {'enabled': False}}}
            (home / 'config.yaml').write_text(json.dumps(cfg))
            env = child_env()
            env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
                OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1',
                HERMES_TEST_ISOLATION=str(home), SUPERVISOR_BOUNDARY=mode,
                GATEWAY_RELAY_URL=f'ws://127.0.0.1:{next(iter(relay.sockets)).getsockname()[1]}/relay',
                GATEWAY_RELAY_PLATFORMS='telegram', GATEWAY_RELAY_BOT_IDS='{"telegram":{"botId":"fixture-bot"}}',
                GATEWAY_RELAY_ID='fixture-gateway', GATEWAY_RELAY_SECRET='fixture-secret')
            with (home / 'daemon.log').open('w+') as log:
                launch = ([sys.executable, str(root / 'tests/gateway/fixtures/supervisor_boundary_daemon.py')]
                          if race else [sys.executable, '-m', 'gateway.run'])
                try:
                    async with budget.phase('initial owner launch'):
                        proc = await asyncio.create_subprocess_exec(*launch, cwd=root, env=env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                    ws = await budget.wait('initial relay ready', asyncio.wait_for(ready.get(), 35))
                    desc = await budget.wait('initial owner ready', _identify_until_ready(home, 35))
                    assert desc.get('state') == 'ready', desc
                    async def send(identity, text):
                        await budget.wait(f'send {identity}', ws.send(json.dumps({'type': 'inbound', 'event': {
                            'text': text, 'message_id': identity,
                            'source': {'platform': 'telegram', 'chat_id': 'automation-chat',
                            'chat_type': 'dm', 'user_id': 'fixture-user',
                            **({'thread_id': '103'} if model.checkin else {})}}}) + '\n'))
                    await send('spawn', 'SPAWN_AUTOMATION' if spawn_terminal else 'WARM_AUTOMATION')
                    await wait_for('warm-up admission settled', lambda: any(r['request_id'] == 'spawn' and r['status'] == 'terminal' for r in rows()))
                    if spawn_terminal:
                        assert 'REAL_TERMINAL_COMPLETION' in json.dumps(model.requests), 'model did not request real terminal'
                    elif mode == 'heartbeat':
                        await send('timer-config', '/heartbeat every 1m TIMER_AUTOMATION')
                        await wait_for('heartbeat configuration reply', lambda: 'Heartbeat' in json.dumps(outgoing))
                    sid = rows()[0]['target_session_id']
                    if peer_checkin:
                        instance = await budget.wait('peer goal wait armed', _arm_peer_goal_wait(root, env, home, desc, sid))
                    if model.checkin:
                        def state():
                            with sqlite3.connect(home / 'state.db') as db:
                                row = db.execute('SELECT value FROM state_meta WHERE key=?', ('checkin:' + sid,)).fetchone()
                                return json.loads(row[0]) if row and row[0] else None
                        # Native execution settles before transport final delivery. Wait for
                        # the acknowledged initial reply's timer reset before advancing it.
                        await wait_for('check-in timer armed', lambda: state() is not None and 'initial' in outbound_at
                                       and state()['last_visible_at'] >= outbound_at['initial'])
                        armed = state()
                        # Advance only this test timer, not the daemon's clock or production config.
                        armed['due_at'] = time.time() - 1
                        with sqlite3.connect(home / 'state.db') as db:
                            db.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(armed), 'checkin:' + sid))
                        if race:
                            await wait_for('check-in race barrier', (home / 'boundary-ready').exists)
                            await send('human-race', 'HUMAN_FIFO_FOLLOWER')
                            await wait_for('check-in race human seen', (home / 'human-seen').exists)
                            if mode == 'checkin-race-execution':
                                await wait_for('check-in race human admitted',
                                               lambda: any(r['request_id'] == 'human-race' for r in rows()))
                            (home / 'boundary-release').touch()
                            await wait_for('check-in race human settled',
                                           lambda: any(r['request_id'] == 'human-race' and r['status'] == 'terminal'
                                                      for r in rows()))
                            automatic = [r for r in rows() if 'supervisor-checkin' in r['request_id']]
                            assert len(automatic) == 1 and automatic[0]['status'] == 'terminal', automatic
                            assert model.checkin_turns == 0, model.requests
                            # The native finite owner settles a no-response discard as completed.
                            # Expendability means no model/send and no FIFO pause, not a new outcome.
                            assert automatic[0]['outcome'] == 'completed', automatic
                            assert not any('SUPERVISOR_STATUS' in json.dumps(f) for f in outgoing), outgoing
                            await send('after-race', 'AFTER_RACE')
                            await wait_for('after-race turn settled',
                                           lambda: any(r['request_id'] == 'after-race' and r['status'] == 'terminal'
                                                      for r in rows()))
                            print(json.dumps({'boundary': mode, 'ledger': rows(), 'model_checkin_turns': 0,
                                              'human_follower_drained': True, 'successor_drained': True}))
                            return
                        try:
                            await wait_for('supervisor status reply', lambda: status_marker in json.dumps(outgoing))
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
                        assert before_ack['fired'] and before_ack['due_at'] == armed['due_at'], {
                            'before_ack': before_ack, 'armed': armed, 'outgoing': outgoing}
                        assert before_ack['last_visible_at'] == armed['last_visible_at'], before_ack
                        assert before_ack['next_attempt_at'] > time.time() + 1700, before_ack
                        status_ack.set()
                        await wait_for('supervisor status acknowledged',
                                       lambda: state()['last_visible_at'] >= outbound_at['supervisor'])
                        assert state()['due_at'] > time.time() + 1700
                        status_frame = next(f for f in outgoing if status_marker in json.dumps(f))
                        assert status_frame['action']['chat_id'] == 'automation-chat', status_frame
                        assert str(status_frame['action']['metadata']['thread_id']) == '103', status_frame
                        assert status_frame['platform'] == 'telegram' and status_frame['botId'] == 'fixture-bot', status_frame
                        if model.checkin_failure:
                            with sqlite3.connect(home / 'state.db') as db:
                                saved = json.loads(db.execute('SELECT value FROM state_meta WHERE key=?',
                                    ('gateway.admission.result.v1.' + automatic[0]['admission_id'],)).fetchone()[0])
                            result = saved['result']
                            assert result['completed'] is False, result
                            assert result['failure_reason'], result
                            for key in ('final_response', 'delivery_text', '_model_final_response'):
                                assert result[key].startswith('Still outstanding:'), result
                            assert 'No reply:' not in json.dumps(status_frame), status_frame
                            assert 'Send it again' not in json.dumps(status_frame), status_frame
                        if peer_checkin:
                            async with budget.phase('peer goal cleared'), websocket(home, desc) as parent:
                                await rpc(parent, 'session.resume', session_id=sid)
                                goal = functools.partial(_goal_call, parent, sid)
                                unchanged = await goal('state')
                                assert len(json.loads(unchanged['state'])['pending_peers']) == 1
                                refused = await goal('resolve_peer', 'hermes:default/idle-peer', 'existing-peer-work',
                                                     'pytest artifact: 1 passed', expected_instance=instance)
                                assert not refused['value']['ok'], refused
                                assert state() is not None
                                await goal('clear')
                            await wait_for('check-in disarmed', lambda: state() is None)
                            print(json.dumps({'peer_only': True, 'unverified_idle_still_pending': True,
                                'supervisor_turns': model.checkin_turns, 'status_delivered': True,
                                'explicit_user_clear_disarmed': True, 'topic': '103'}))
                            return
                        model.gate.touch()
                        await wait_for('terminal completion status reply', lambda: 'COMPLETION_STATUS' in json.dumps(outgoing))
                        await wait_for('check-in disarmed', lambda: state() is None)
                        assert model.checkin_turns == 1
                        print(json.dumps({'supervisor_turns': model.checkin_turns, 'status_delivered': True,
                                          'completion_disarmed': True, 'topic': '103'}))
                        return
                    async with budget.phase('observer resumed'), websocket(home, desc) as observer:
                        assert 'result' in await rpc(observer, 'session.resume', session_id=sid)
                    if mode.startswith('restart-'):
                        # Do not let a late reply to the warm-up turn count as the recovered reply.
                        await wait_for('warm-up reply', lambda: 'initial' in outbound_at)
                        model.gate.touch()
                        assert await wait_for_model('terminal completion entered model', model.completion_blocked, 20)
                        automatic = [r for r in rows() if r['principal_id'].startswith('automation:')]
                        assert len(automatic) == 1 and automatic[0]['status'] == 'started', rows()
                        await send('human-follower', 'HUMAN_FIFO_FOLLOWER')
                        def follower_queued():
                            return any(r['request_id'] == 'human-follower' and r['status'] == 'queued'
                                       and r['target_session_id'] == sid for r in rows())

                        await wait_for('human follower queued', follower_queued)
                        old_pid, old_epoch = proc.pid, desc['authority_epoch']
                        old_ws = ws
                        sent_before_restart = len(outgoing)
                        old_proc, proc = proc, None  # Teardown may signal it, but never waits again.
                        returncode = await budget.wait(f'old owner PID {old_pid} exit reap',
                                                       _kill_and_reap(old_proc, 'old owner'))
                        old_proc = None
                        if os.name == 'nt':
                            assert returncode == 1, returncode  # TerminateProcess exit code.
                        else:
                            assert returncode == -signal.SIGKILL, returncode  # windows-footgun: ok
                        if mode == 'restart-started':
                            # Inject the delayed busy frame at the relay observation boundary.
                            # Its order relative to the successor's real send must not depend on load.
                            outgoing.append({'type': 'outbound', 'action': {'op': 'send',
                                'content': '⏳ Queued for the next turn', 'chat_id': 'automation-chat'}})
                        model.hold_completion = False
                        model.completion_release.set()
                        if mode == 'restart-denied':
                            env['GATEWAY_RELAY_SECRET'] = 'changed-fixture-secret'
                        async with budget.phase('successor owner launch'):
                            proc = await asyncio.create_subprocess_exec(
                                sys.executable, '-m', 'gateway.run', cwd=root, env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                        ws = successor_ws = await budget.wait('successor relay ready', asyncio.wait_for(ready.get(), 35))
                        desc = await budget.wait('successor owner ready', _identify_until_ready(home, 35))
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
                        await wait_for('successor ledger settled', lambda: all(r['status'] == 'terminal' for r in rows()))
                        recovered = [r for r in rows() if r['principal_id'].startswith('automation:')]
                        assert len(recovered) == 1 and recovered[0]['outcome'] == 'interrupted', rows()
                        assert recovered[0]['admission_id'] == automatic[0]['admission_id']
                        follower = next(r for r in rows() if r['request_id'] == 'human-follower')
                        assert follower['outcome'] == 'completed', follower

                        def replies(marker):
                            return [f['action'] for f in outgoing[sent_before_restart:]
                                if f.get('type') == 'outbound' and f.get('action', {}).get('op') == 'send'
                                and f['action'].get('content') == marker]

                        # The first predicate check sees a busy notice and a settled ledger, but
                        # cannot see the real reply until it yields back to the relay connector.
                        def expose_replies():
                            nonlocal expose_successor_reply
                            expose_successor_reply = True
                            outgoing.extend(delayed_replies)
                            delayed_replies.clear()

                        asyncio.get_running_loop().call_soon(expose_replies)
                        await wait_for('successor follower reply', lambda: replies(_FOLLOWER_ACK))
                        await send('after-recovery', 'AFTER_RECOVERY')
                        # A terminal row can precede delivery. Observe both within the later
                        # turn's own allowance, separate from the follower reply.
                        async with budget.phase('after-recovery turn'), asyncio.timeout(35):
                            await async_wait_until(lambda: any(r['request_id'] == 'after-recovery'
                                and r['status'] == 'terminal' and r['outcome'] == 'completed'
                                and r['seq'] > follower['seq'] for r in rows()), timeout=35)
                            await async_wait_until(lambda: any(reply_sources[id(action)] is successor_ws
                                                               for action in replies(_AFTER_RECOVERY_ACK)), timeout=35)
                        from gateway.status import write_planned_stop_marker

                        new_pid = proc.pid
                        with patch.dict(os.environ, HERMES_HOME=str(home)):
                            stop_requested = write_planned_stop_marker(new_pid)
                        assert stop_requested, new_pid
                        # Once idle is acknowledged, the relay reader only has to consume
                        # already-buffered frames, so the shutdown drain needs no turn budget.
                        await budget.wait('successor relay drained', asyncio.wait_for(successor_relay_drained.wait(), 10))
                        assert successor_idle_ack.is_set(), outgoing[sent_before_restart:]
                        returncode = await budget.wait(f'successor owner PID {new_pid} exit reap',
                                                       asyncio.wait_for(proc.wait(), 10))
                        proc = None
                        assert returncode == 0, returncode
                        replayed_sends = replies(_AUTOMATION_ACK)
                        assert not replayed_sends, f'unexpected post-restart AUTOMATION_ACK sends: {replayed_sends!r}'
                        revived_sends = replies(_FOLLOWER_ACK)
                        assert len(revived_sends) == 1, outgoing[sent_before_restart:]
                        assert reply_sources[id(revived_sends[0])] is successor_ws, revived_sends
                        assert revived_sends[0].get('metadata', {}).get('user_id') == 'fixture-user', revived_sends
                        texts = [next((m.get('content', '') for m in reversed(r['messages'])
                            if m['role'] == 'user'), '') for r in model.requests if r.get('messages')]
                        assert sum('REAL_TERMINAL_COMPLETION' in str(t) for t in texts) == 1, texts
                        assert sum('HUMAN_FIFO_FOLLOWER' in str(t) for t in texts) == 1, texts
                        assert sum('AFTER_RECOVERY' in str(t) for t in texts) == 1, texts
                        with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as db:
                            notices = db.execute("SELECT content FROM messages WHERE session_id=? AND role='assistant'",
                                                 (sid,)).fetchall()
                        assert any('was interrupted and was not run again' in (text or '')
                                   for (text,) in notices), notices
                        print(json.dumps({'old_pid': old_pid, 'new_pid': new_pid, 'ledger': rows(),
                                          'model_inputs': texts, 'interruption_notices': notices}))
                        return
                    await send('held', 'HOLD_AUTOMATION')
                    assert await wait_for_model('held turn entered model', model.blocked, 15)
                    await send('human-follower', 'HUMAN_FIFO_FOLLOWER')
                    await wait_for('human follower admitted', lambda: any(r['request_id'] == 'human-follower' for r in rows()))
                    model.gate.touch()
                    # Real cadence: process polls every five seconds; heartbeat interval is one minute.
                    # Poll for the producer's commit instead of sleeping out the whole bound.
                    async with budget.phase('automation producer committed'):
                        producer_deadline = time.monotonic() + (8 if mode == 'terminal' else 70)
                        while (time.monotonic() < producer_deadline
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
                    await wait_for('ledger settled', lambda: all(r['status'] == 'terminal' for r in rows()))
                    assert all(r['outcome'] == 'completed' for r in rows()), rows()
                    texts = [next((m.get('content', '') for m in reversed(r['messages']) if m['role'] == 'user'), '')
                             for r in model.requests if r.get('messages')]
                    marker = 'REAL_TERMINAL_COMPLETION' if mode == 'terminal' else 'TIMER_AUTOMATION'
                    assert marker in str(texts[-1]), texts
                    assert 'HUMAN_FIFO_FOLLOWER' in str(texts[-2]), texts
                    print(json.dumps({'daemon_pid': proc.pid, 'ledger': rows(), 'model_inputs': texts,
                                      'observer_closed_before_completion': True}))
                except BaseException as primary:
                    status_ack.set()
                    try:
                        (home / 'boundary-release').touch()
                        if (home / 'state.db').exists():
                            print(json.dumps({'old_pid': old_pid, 'ledger_at_failure': rows(), 'outgoing': outgoing,
                                'model_inputs': [next((m.get('content', '') for m in reversed(r['messages'])
                                    if m['role'] == 'user'), '') for r in model.requests if r.get('messages')]}),
                                file=sys.stderr)
                        print((home / 'daemon.log').read_text()[-24000:], file=sys.stderr)
                        for name in ('logs/gateway.log', 'sessions/sessions.json', 'sessions.json'):
                            diagnostic = home / name
                            if diagnostic.is_file():
                                print(name + '\n' + diagnostic.read_text()[-24000:], file=sys.stderr)
                    except (OSError, sqlite3.Error) as exc:
                        primary.add_note(f'Could not collect fixture diagnostics: {exc!r}')
                    raise
    finally:
        primary = sys.exception()
        try:
            if old_proc is not None and old_proc.returncode is None:
                # Preserve the first failure and leave reaping to the watcher.
                assert primary is not None
                primary.add_note(f'Old owner PID {old_proc.pid} still unreaped at teardown; sending best-effort kill')
                _signal_owner(old_proc)
            if proc is not None:
                await _kill_and_reap(proc, 'fixture cleanup')
        except Exception as exc:
            if primary is None:
                raise
            primary.add_note(f'Owner cleanup failed: {exc!r}')
        finally:
            active_error = sys.exception()
            try:
                model.release.set()
                model.completion_release.set()
                try:
                    model.gate.touch()
                finally:
                    model.shutdown()
                    model.server_close()
            except Exception as exc:
                if active_error is None:
                    raise
                active_error.add_note(f'Model cleanup failed: {exc!r}')


if __name__ == '__main__':
    asyncio.run(probe(Path(__file__).resolve().parents[3], Path(sys.argv[1]), sys.argv[2],
                      deadline=float(sys.argv[3])))
