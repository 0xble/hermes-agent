"""Normal-policy compatibility and fail-closed controls for private maintenance."""
import asyncio
import json

import pytest

from agent.incognito import incognito_scope, is_incognito
from tools import mcp_tool as core
from tools.mcp_tool import MCPServerTask
from tools.mcp_tool_callback_context import private_rpc_transport
from tools.mcp_tool_scope import _server_key
from tests.tools.test_mcp_private_compatibility import compat_http as _registered_compat_http, native_http as _registered_native_http, expire

native_http = _registered_native_http

compat_http = _registered_compat_http
from tests.tools.test_mcp_private_oauth_transport import config, read
from tests.tools.mcp_private_native_http import PRIVATE, NORMAL


@pytest.mark.parametrize('kind', ['sse'])
def test_legacy_grant_without_prm_keeps_normal_resource_policy(compat_http, kind):
    f = compat_http
    cfg = config(kind)
    cfg['headers'] = {'MCP-Protocol-Version': '2024-11-05'}
    # This legacy AS rejects any unexpected resource parameter, not just foreign values.
    original = f.respond
    async def legacy_endpoint(request):
        from urllib.parse import parse_qs
        body = await request.aread()
        if '.well-known/oauth-protected-resource' in request.url.path:
            return f.http.Response(404)
        params = parse_qs(body.decode()) if request.url.path == '/token' else {}
        if params.get('grant_type') == ['refresh_token'] and 'resource' in params:
            return f.http.Response(400, json={'error': 'invalid_target'})
        return await original(request)
    f.respond = legacy_endpoint
    async def exercise():
        server = MCPServerTask('synthetic-legacy')
        with f.consent():await server.start(cfg)
        try:
            provider = next(iter(f.manager._entries.values())).provider
            assert (await read(server)).contents[0].text == NORMAL
            assert provider.context.protected_resource_metadata is None
            assert provider.context.storage.load_oauth_resource() is None
            expire(provider)
            try:
                with incognito_scope(True):
                    async with private_rpc_transport(server) as target:result = await read(target)
            except Exception as exc:result = exc
            assert (await read(server)).contents[0].text == NORMAL
            assert not isinstance(result, Exception), result
            assert result.contents[0].text == PRIVATE
            assert provider._hermes_fence is None
        finally:await server.shutdown()
    asyncio.run(exercise())


def test_foreign_saved_audience_fails_before_refresh_without_private_repair(compat_http):
    f = compat_http
    async def exercise():
        server = MCPServerTask('synthetic-invalid-audience')
        with f.consent():await server.start(config('http'))
        try:
            provider = next(iter(f.manager._entries.values())).provider
            assert (await read(server)).contents[0].text == NORMAL
            expire(provider)
            from tools.mcp_oauth import _read_json, _write_json
            path = provider.context.storage._meta_path()
            metadata = _read_json(path)
            metadata['hermes_resource'] = 'https://foreign.synthetic.invalid/'
            _write_json(path, metadata)
            before = f.snapshot()
            with pytest.raises(Exception):
                with incognito_scope(True):
                    async with private_rpc_transport(server) as target:await read(target)
            after = f.snapshot()
            lock = 'mcp-tokens/synthetic-invalid-audience.json.refresh.lock'
            assert after.pop(lock, '') == ''
            assert after == before
            assert not [r for r in f.boundary if r['kind'] == 'audience' and r['private']]
            assert provider._hermes_fence is None
            assert PRIVATE not in f.log()
        finally:await server.shutdown()
    asyncio.run(exercise())


def test_normal_first_use_events_list_keeps_shared_lazy_connect(compat_http, monkeypatch):
    from tools import mcp_tool_discovery as discovery, mcp_tool_events as events
    f = compat_http
    async def exercise():
        warm = MCPServerTask('synthetic-first-use-events')
        cfg = config('http')
        with f.consent():await warm.start(cfg)
        await warm.shutdown()
        key = _server_key(warm.name)
        monkeypatch.setattr(core, '_mcp_loop', asyncio.get_running_loop())
        monkeypatch.setitem(core._lazy_server_configs, key, cfg)
        # Keep the real discovery/first-use path. Only observe it; do not emulate its effects.
        calls = []
        original = discovery._ensure_lazy_server_connected
        def observe(name):
            calls.append((name, is_incognito()))
            return original(name)
        monkeypatch.setattr(discovery, '_ensure_lazy_server_connected', observe)
        handler = events._make_list_event_types_handler(warm.name, 8)
        try:
            result = json.loads(await asyncio.to_thread(handler, {}))
            assert result.get('events') == [{'name': NORMAL}], result
            assert calls == [(warm.name, False)]
            assert core._servers[key].session is not None
            assert key not in core._lazy_server_configs
        finally:
            if server := core._servers.get(key):await server.shutdown()
    asyncio.run(exercise())
