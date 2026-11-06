"""Production handler/SDK/one-shot health and at-most-once write contracts, offline."""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import sys
import threading

import psutil
import pytest

from agent.incognito import incognito_scope, install_logging_filter
from tools import mcp_tool as core
from tools.mcp_tool import MCPServerTask
from tools.mcp_tool_handlers import _make_tool_handler
from tools.mcp_tool_scope import _server_key
from tests.tools.mcp_private_native_http import native_http as _registered_native_http, PRIVATE, NORMAL, snapshot

native_http = _registered_native_http


@pytest.fixture
def stdio_tools(tmp_path, monkeypatch):
    core._ensure_mcp_sdk()
    install_logging_filter()
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    sink = tmp_path / 'handler.log'
    handler = logging.FileHandler(sink)
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    effects = tmp_path / 'effects.jsonl'
    config = {'command': sys.executable, 'args': [str(Path(__file__).with_name('mcp_private_native_tools_child.py')), str(effects)],
              'env': dict(os.environ), 'protocol': 'legacy', 'connect_timeout': 3}
    threads_before = set(threading.enumerate())
    pids = []
    observed_results = []
    @asynccontextmanager
    async def connected():
        server = MCPServerTask('synthetic-handler-health')
        await server.start(config)
        key = _server_key(server.name)
        monkeypatch.setitem(core._servers, key, server)
        monkeypatch.setattr(core, '_mcp_loop', asyncio.get_running_loop())
        call = _make_tool_handler(server.name, 'write', 8)
        async def invoke(mode='success', private=False):
            with incognito_scope(private):
                result = json.loads(await asyncio.to_thread(call, {'mode': mode, 'marker': PRIVATE if private else NORMAL}))
                observed_results.append({'private': private, 'mode': mode, 'result': result})
                return result
        try:yield server, key, invoke
        finally:
            target = core._servers.get(key)
            if target is not None and target is not server:await target.shutdown()
            await server.shutdown()
    def rows():return [json.loads(line) for line in effects.read_text().splitlines()] if effects.exists() else []
    try:yield connected, rows, config
    finally:
        pids = sorted({r['pid'] for r in rows()})
        from tools.mcp_tool_config import _close_mcp_stderr_logs
        from hermes_constants import hermes_home_key
        _close_mcp_stderr_logs(scope=hermes_home_key())
        for thread in set(threading.enumerate()) - threads_before:
            if thread.name == 'mcp-stderr':
                thread.join(3)
                assert not thread.is_alive()
        handler.flush()
        root.removeHandler(handler)
        handler.close()
        root.setLevel(previous)
        assert not any(psutil.pid_exists(pid) for pid in pids)
        assert PRIVATE not in sink.read_text()
        assert PRIVATE.encode().hex() not in json.dumps(snapshot(home))
        if output := os.environ.get('AUX_RUN_DIR'):
            dest = Path(output) / tmp_path.name
            dest.mkdir()
            (dest / 'disk.json').write_text(json.dumps(snapshot(home), indent=2))
            (dest / 'sink.log').write_bytes(sink.read_bytes())
            (dest / 'effects.json').write_text(json.dumps(rows()))
            (dest / 'handler-results.json').write_text(json.dumps(observed_results, indent=2))
            (dest / 'teardown.json').write_text(json.dumps({'pids': pids, 'alive': [p for p in pids if psutil.pid_exists(p)]}))


@pytest.mark.parametrize('mode', ['exit', 'pipe-close'])
def test_private_stdio_write_loss_is_uncertain_once_with_normal_control(stdio_tools, mode):
    connected, rows, _ = stdio_tools
    async def exercise():
        async with connected() as (_server, _key, invoke):
            before = len(rows())
            result = await invoke(mode, private=True)
            private_effects = len(rows()) - before
            # Normal control is exercised even on the broken candidate, before asserting RED.
            normal = await invoke()
            assert normal.get('result') == NORMAL
            assert private_effects == 1, result
            assert result.get('outcome_uncertain') is True, result
            assert any(word in result['error'].lower() for word in ('verify', 'inspect')) and any(word in result['error'].lower() for word in ('retry', 'retried', 're-invoking'))
    asyncio.run(exercise())


@pytest.mark.parametrize('mode', ['write-loss', 'write-expired'])
def test_private_http_write_loss_is_uncertain_once_with_normal_control(native_http, mode, monkeypatch):
    from tests.tools.test_mcp_private_oauth_transport import config
    f = native_http
    async def exercise():
        server = MCPServerTask('synthetic-http-write')
        with f.consent():await server.start(config('http'))
        key = _server_key(server.name)
        monkeypatch.setitem(core._servers, key, server)
        monkeypatch.setattr(core, '_mcp_loop', asyncio.get_running_loop())
        call = _make_tool_handler(server.name, 'write', 8)
        try:
            f.current.case = mode
            with incognito_scope(True):result = json.loads(await asyncio.to_thread(call, {'marker': PRIVATE}))
            effects = len(f.effects)
            f.current.case = 'valid'
            normal = json.loads(await asyncio.to_thread(call, {'marker': NORMAL}))
            assert normal.get('result') == NORMAL
            assert effects == 1, result
            assert result.get('outcome_uncertain') is True, result
            assert any(word in result['error'].lower() for word in ('verify', 'inspect')) and any(word in result['error'].lower() for word in ('retry', 'retried', 're-invoking'))
            assert PRIVATE not in f.log()
            assert PRIVATE.encode().hex() not in json.dumps(f.snapshot())
        finally:
            target = core._servers.get(key)
            if target is not None and target is not server:await target.shutdown()
            await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('case', ['success', 'error', 'startup-failures', 'public-open', 'public-offline'])
def test_private_tool_health_is_independent_and_normal_bookkeeping_resumes(stdio_tools, case):
    connected, _rows, config = stdio_tools
    async def exercise():
        async with connected() as (server, key, invoke):
            # Nonzero/unproven is material: a private success must not reset/prove this lifetime.
            core._server_error_counts[key] = 1
            core._server_errors_all_application[key] = False
            server._session_proven = False
            server._reconnect_retries = 2
            before_health = (core._server_error_counts[key], server._session_proven, server._reconnect_retries, server._last_tool_call_at)
            if case == 'startup-failures':
                core._server_error_counts[key] = 0
                original = server._config
                server._config = dict(config, args=config['args'] + ['--startup-fail'])
                try:private_results = [await invoke(private=True) for _ in range(3)]
                finally:server._config = original
                after_private = core._server_error_counts[key]
                normal = await invoke()
                assert normal.get('result') == NORMAL, (private_results, normal)
                assert all('error' in result and not result.get('outcome_uncertain') for result in private_results), private_results
                assert after_private == 0, private_results
            else:
                if case == 'public-open':
                    import time
                    core._server_error_counts[key] = core._CIRCUIT_BREAKER_THRESHOLD
                    core._server_breaker_opened_at[key] = time.monotonic()
                if case == 'public-offline':
                    await server.shutdown()
                    # A config carrier is enough for a private connection: public readiness is irrelevant.
                state = (core._server_error_counts[key], server._session_proven, server._reconnect_retries, server._last_tool_call_at)
                result = await invoke('error' if case == 'error' else 'success', private=True)
                observed = (core._server_error_counts[key], server._session_proven, server._reconnect_retries, server._last_tool_call_at)
                if case == 'public-open':core._server_breaker_opened_at.pop(key, None);core._server_error_counts[key] = 1
                if case == 'public-offline':
                    server = MCPServerTask('synthetic-handler-health')
                    await server.start(config)
                    core._servers[key] = server
                normal = await invoke()
                assert normal.get('result') == NORMAL
                assert ('error' in result) if case == 'error' else result.get('result') == PRIVATE, result
                assert observed == state, (before_health, state, observed)
            assert core._server_error_counts[key] == 0
            assert server._session_proven
            # A real normal application failure must still charge the normal breaker.
            assert 'error' in await invoke('error')
            assert core._server_error_counts[key] == 1
            if case == 'public-offline':await server.shutdown()
    asyncio.run(exercise())
