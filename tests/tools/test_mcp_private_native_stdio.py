"""Native SDK subprocesses, same-name one-shot lifetimes, actual stderr and PID ledger."""
import asyncio
import json
import os
import sys
from pathlib import Path

import psutil
import pytest

from agent.incognito import incognito_scope, install_logging_filter
from tools import mcp_tool as core
from tools.mcp_tool import MCPServerTask
from tools.mcp_tool_callback_context import private_rpc_transport
from tests.tools.mcp_private_native_http import snapshot, PRIVATE, NORMAL


@pytest.mark.parametrize('case', ['success', 'failure', 'cancel'])
def test_owned_private_stdio_preserves_normal_incarnation_and_no_private_header(tmp_path, monkeypatch, case):
    import logging
    import threading
    threads_before = set(threading.enumerate())
    core._ensure_mcp_sdk()
    install_logging_filter()
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    env = dict(os.environ)
    config = {'command': sys.executable, 'args': [str(Path(__file__).with_name('mcp_private_native_child.py'))],
              'env': env, 'protocol': 'legacy', 'connect_timeout': 3}
    sink = tmp_path / 'stdio.log'
    handler = logging.FileHandler(sink)
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    pids = []
    async def read(server, marker):
        return await asyncio.wait_for(server.session.read_resource('fixture://' + marker), 5)
    async def exercise():
        server = MCPServerTask('synthetic-native-same-name')
        await server.start(config)
        try:
            public = await read(server, NORMAL)
            public_pid = json.loads(public.contents[0].text)['pid']
            pids.append(public_pid)
            assert psutil.Process(public_pid).is_running()
            original_config = server._config
            with incognito_scope(True):
                server._config = dict(config, args=config['args'] + [PRIVATE])
                try:
                    async with private_rpc_transport(server) as private:
                        result = await read(private, PRIVATE)
                        private_pid = json.loads(result.contents[0].text)['pid']
                        pids.append(private_pid)
                        assert private_pid != public_pid
                        if case == 'failure':
                            with pytest.raises(Exception):await read(private, PRIVATE + '/failure')
                        if case == 'cancel':raise asyncio.CancelledError
                except asyncio.CancelledError:
                    if case != 'cancel':raise
                finally:server._config = original_config
            # Closing a private connection may not sweep the public same-name incarnation.
            assert psutil.Process(public_pid).is_running(), 'one-shot lifetime killed same-name normal child'
            again = await read(server, NORMAL)
            assert json.loads(again.contents[0].text)['pid'] == public_pid
        finally:await server.shutdown()
    try:
        asyncio.run(exercise())
        handler.flush()
        assert not any(psutil.pid_exists(pid) for pid in pids), 'owned MCP children survived teardown'
        captured = snapshot(home)
        assert not any(PRIVATE.encode().hex() in v for v in captured.values()), 'private launch header/stderr leaked into actual owned home'
        assert NORMAL.encode().hex() in json.dumps(captured), 'normal stderr did not persist/resume'
        assert PRIVATE not in sink.read_text()
    finally:
        from tools.mcp_tool_config import _close_mcp_stderr_logs
        from hermes_constants import hermes_home_key
        _close_mcp_stderr_logs(scope=hermes_home_key())
        for thread in set(threading.enumerate()) - threads_before:
            if thread.name == 'mcp-stderr':
                thread.join(3)
                assert not thread.is_alive(), 'owned stderr pump survived teardown'
        root.removeHandler(handler)
        handler.close()
        root.setLevel(level)
        if output := os.environ.get('AUX_RUN_DIR'):
            dest = Path(output) / tmp_path.name
            dest.mkdir()
            (dest / 'disk.json').write_text(json.dumps(snapshot(home), indent=2))
            (dest / 'sink.log').write_bytes(sink.read_bytes())
            (dest / 'pids.json').write_text(json.dumps({'pids': pids, 'alive': [pid for pid in pids if psutil.pid_exists(pid)]}))



def test_normal_cross_process_refresh_rotation_fence_is_still_functional(tmp_path, monkeypatch):
    import subprocess
    import selectors
    from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken
    from tools.mcp_oauth import HermesTokenStorage, _read_json, _write_json
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    storage = HermesTokenStorage('synthetic-cross-process')
    async def seed():
        storage.bind_issuer('https://refresh.synthetic.invalid')
        await storage.set_tokens(OAuthToken(access_token='synthetic-old-access', refresh_token='synthetic-r0',
                                            token_type='Bearer', expires_in=3600))
        await storage.set_client_info(OAuthClientInformationFull(client_id='synthetic-normal-client',
            redirect_uris=['https://callback.synthetic.invalid/callback'], token_endpoint_auth_method='none'))
        storage.save_oauth_metadata(OAuthMetadata(issuer='https://refresh.synthetic.invalid',
            authorization_endpoint='https://refresh.synthetic.invalid/authorize', token_endpoint='https://refresh.synthetic.invalid/token',
            response_types_supported=['code']))
    asyncio.run(seed())
    raw = _read_json(storage._tokens_path())
    raw['expires_at'] = 1
    _write_json(storage._tokens_path(), raw)
    authority = tmp_path / 'synthetic-authority.json'
    authority.write_text(json.dumps({'posts': 0, 'refresh': 'synthetic-r0'}))
    children, results = [], []
    try:
        for _ in range(2):
            children.append(subprocess.Popen([sys.executable, str(Path(__file__).with_name('mcp_private_native_refresh_child.py')), str(authority)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ)))
        for child in children:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                assert selector.select(15), 'owned refresh worker failed to initialize'
                line = child.stdout.readline()
                assert line == 'ready\n', (line, child.poll())
        for child in children:
            child.stdin.write('go\n')
            child.stdin.flush()
        for child in children:
            stdout, stderr = child.communicate(timeout=20)
            results.append({'pid': child.pid, 'rc': child.returncode, 'stdout': stdout, 'stderr': stderr})
            assert child.returncode == 0, results[-1]
        assert json.loads(authority.read_text())['posts'] == 1, 'normal workers consumed rotating grant twice'
        assert _read_json(storage._tokens_path())['refresh_token'] == 'synthetic-r1'
        from tools.mcp_oauth import _refresh_lock_path
        assert _refresh_lock_path(storage._tokens_path()).exists()
    finally:
        for child in children:
            if child.poll() is None:child.kill()
            child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream and not stream.closed:stream.close()
        if output := os.environ.get('AUX_RUN_DIR'):
            dest = Path(output) / tmp_path.name
            dest.mkdir()
            (dest / 'workers.json').write_text(json.dumps(results, indent=2))
            (dest / 'disk.json').write_text(json.dumps(snapshot(home), indent=2))
