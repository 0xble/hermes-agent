"""Audience-bound renewal, configured HTTP policy and config-only Events reads.

The installed SDK, real AsyncClient routing and Hermes ownership/storage stay real.
Only the network-facing transport is offline: no TLS handshake is claimed here.
"""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from agent.incognito import incognito_scope, is_incognito
from tools import mcp_tool as core
from tools.mcp_tool import MCPServerTask
from tools.mcp_tool_callback_context import private_rpc_transport
from tools.mcp_tool_scope import _server_key
from tests.tools.mcp_private_native_http import native_http as _registered_native_http, PRIVATE, NORMAL, URL, ISSUER

native_http = _registered_native_http
from tests.tools.test_mcp_private_oauth_transport import config, read


@pytest.fixture
def compat_http(native_http, monkeypatch):
    f = native_http
    http = f.http
    import httpx2._client as http_client
    from tools import mcp_tool_transport as transport
    assert http is __import__('httpx2'), 'must exercise the pinned SDK HTTP flavour'
    f.boundary = []
    f.required_resource = None
    f.required_policy = None
    f.proxy = None
    f.transports = []

    async def endpoint(request):
        body = await request.aread()
        params = parse_qs(body.decode()) if request.url.path == '/token' else {}
        if ('.well-known/oauth-protected-resource' in request.url.path and f.required_resource
                and not getattr(f, 'prm_passthrough', False)):
            return http.Response(200, json={'resource': f.required_resource, 'authorization_servers': [ISSUER]})
        if params.get('grant_type') == ['refresh_token']:
            accepted = not f.required_resource or params.get('resource') == [f.required_resource]
            f.boundary.append({'kind': 'audience', 'accepted': accepted, 'params': params,
                               'url': str(request.url), 'private': is_incognito()})
            if not accepted:
                return http.Response(400, json={'error': 'invalid_target'})
        if request.url.path == '/mcp' and request.headers.get('authorization') and body:
            message = json.loads(body)
            if message.get('method') == 'events/list':
                marker = PRIVATE if is_incognito() else NORMAL
                f.records.append({'private': is_incognito(), 'method': 'events/list', 'url': str(request.url)})
                return http.Response(200, json={'jsonrpc': '2.0', 'id': message['id'],
                    'result': {'events': [{'name': marker}]}})
        return await f.respond(request)

    class PolicyTransport(http.AsyncBaseTransport):
        def __init__(self, **kwargs):
            self.policy = kwargs
            self.closed = False
            f.transports.append(self)
        async def handle_async_request(self, request):
            policy = self.policy
            expected = f.required_policy
            accepted = not expected or all(policy.get(k) == v for k, v in expected.items())
            f.boundary.append({'kind': 'transport', 'accepted': accepted, 'policy': policy,
                               'url': str(request.url), 'private': is_incognito()})
            if not accepted:
                raise http.ConnectError('Synthetic endpoint requires independently configured CA/client/proxy policy', request=request)
            return await endpoint(request)
        async def aclose(self):self.closed = True

    def client_init(client, *args, **kwargs):
        f.original_client_init(client, *args, **kwargs)
        f.clients.append(client)
        if client.auth is not None:f.providers.append(client.auth)
    monkeypatch.setattr(http, 'AsyncHTTPTransport', PolicyTransport)
    monkeypatch.setattr(http_client, 'AsyncHTTPTransport', PolicyTransport)
    monkeypatch.setattr(http.AsyncClient, '__init__', client_init)
    monkeypatch.setattr(transport.urllib.request, 'getproxies', lambda: {'https': f.proxy} if f.proxy else {})
    monkeypatch.setattr(transport.urllib.request, 'proxy_bypass', lambda host: False)
    monkeypatch.setattr(transport, 'should_bypass_proxy', lambda url: False)
    try:yield f
    finally:
        assert all(t.closed for t in f.transports), 'offline transport leaked'
        assert all(s.closed for s in getattr(f, 'challenge_streams', ())), 'independent challenge body leaked'
        if output := os.environ.get('AUX_RUN_DIR'):
            target = Path(output) / ('compat-' + f.tmp.name)
            target.mkdir()
            (target / 'boundary.json').write_text(json.dumps(f.boundary, indent=2, default=str))
            if hasattr(f, 'auth_requests'):
                (target / 'independent-auth-requests.json').write_text(json.dumps(f.auth_requests, indent=2))
            (target / 'teardown.json').write_text(json.dumps({'transports': len(f.transports),
                'closed': sum(t.closed for t in f.transports), 'real_network': False}))


def expire(provider):
    from tools.mcp_oauth import _read_json, _write_json
    path = provider.context.storage._tokens_path()
    raw = _read_json(path)
    raw['expires_at'] = 1
    _write_json(path, raw)
    provider.context.token_expiry_time = -1
    return raw


def poison_private_provider(f, monkeypatch):
    """Challenge/PRM/request authority must never become maintenance authority."""
    original = f.manager.get_or_build_provider
    def build(*args, **kwargs):
        provider = original(*args, **kwargs)
        if not is_incognito():return provider
        initialize = provider._initialize
        async def initialized():
            await initialize()
            from mcp.shared.auth import ProtectedResourceMetadata
            provider.context.server_url = 'https://' + PRIVATE.lower() + '.invalid/foreign-request'
            provider.context.protocol_version = '1900-01-01'
            provider.context.protected_resource_metadata = ProtectedResourceMetadata(
                resource='https://' + PRIVATE.lower() + '.invalid/foreign-resource',
                authorization_servers=['https://' + PRIVATE.lower() + '.invalid/foreign-issuer'],
                scopes_supported=[PRIVATE])
            provider.context.client_metadata.scope = PRIVATE
            # Keep the injected endpoint a valid SDK URL value; model_copy with
            # a raw str bypasses Pydantic validation and produces test-generated
            # serialization warnings containing the marker outside product logs.
            metadata = provider.context.oauth_metadata.model_dump(mode='json')
            metadata['token_endpoint'] = 'https://' + PRIVATE.lower() + '.invalid/foreign-token'
            provider.context.oauth_metadata = type(provider.context.oauth_metadata).model_validate(metadata)
        provider._initialize = initialized
        return provider
    monkeypatch.setattr(f.manager, 'get_or_build_provider', build)


@pytest.mark.parametrize('kind,readers,audience', [('http', 1, URL), ('sse', 1, URL), ('http', 2, URL), ('http', 1, ISSUER + '/')])
def test_saved_audience_renewal_ignores_private_authority_and_normal_resumes(compat_http, monkeypatch, kind, readers, audience):
    f = compat_http
    f.required_resource = audience
    async def exercise():
        server = MCPServerTask('synthetic-audience')
        with f.consent():await server.start(config(kind))
        try:
            provider = next(iter(f.manager._entries.values())).provider
            assert (await read(server)).contents[0].text == NORMAL
            original = expire(provider)
            poison_private_provider(f, monkeypatch)
            async def private_read():
                try:
                    with incognito_scope(True):
                        async with private_rpc_transport(server) as target:return await read(target)
                except Exception as exc:return exc
            results = await asyncio.gather(*(private_read() for _ in range(readers)))
            private_boundary = [r for r in f.boundary if r['kind'] == 'audience' and r['private']]
            # Exercise supported normal renewal even when private renewal is broken.
            assert (await read(server)).contents[0].text == NORMAL
            from tools.mcp_oauth import _read_json
            saved = _read_json(provider.context.storage._tokens_path())
            assert saved['refresh_token'] != original['refresh_token']
            assert provider._hermes_fence is None
            assert PRIVATE not in f.log()
            assert PRIVATE.encode().hex() not in json.dumps(f.snapshot())
            assert all(not isinstance(r, Exception) for r in results), results
            assert all(r.contents[0].text == PRIVATE for r in results)
            assert len(private_boundary) == 1 and private_boundary[0]['accepted'], private_boundary
            params = private_boundary[0]['params']
            assert params['resource'] == [audience] and PRIVATE not in json.dumps(params)
            assert private_boundary[0]['url'] == ISSUER + '/token'
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('kind', ['http', 'sse'])
@pytest.mark.parametrize('policy', ['default', 'ca', 'mtls', 'proxy'])
def test_saved_renewal_uses_normal_configured_transport(compat_http, kind, policy):
    f = compat_http
    cfg = config(kind)
    if policy != 'default':
        ca = f.tmp / 'synthetic-ca.pem'
        ca.write_text('offline boundary fixture, not a real certificate')
        cfg['ssl_verify'] = str(ca)
        f.required_policy = {'verify': str(ca)}
    else:f.required_policy = {'verify': True}
    if policy in ('mtls', 'proxy'):
        cert = f.tmp / 'synthetic-client.pem'
        cert.write_text('offline boundary fixture, not a real certificate')
        cfg['client_cert'] = str(cert)
        f.required_policy['cert'] = str(cert)
    if policy == 'proxy':
        f.proxy = 'http://proxy.synthetic.invalid:8443'
        f.required_policy['proxy'] = f.proxy
    # Normal transport config is independently persisted, never taken from a private request.
    (f.home / 'config.yaml').write_text(json.dumps({'mcp_servers': {'synthetic-policy': cfg}}))
    async def exercise():
        server = MCPServerTask('synthetic-policy')
        with f.consent():await server.start(cfg)
        try:
            provider = next(iter(f.manager._entries.values())).provider
            assert (await read(server)).contents[0].text == NORMAL
            expire(provider)
            try:
                with incognito_scope(True):
                    async with private_rpc_transport(server) as target:result = await read(target)
            except Exception as exc:result = exc
            observed = [r for r in f.boundary if r['kind'] == 'transport' and r['private'] and r['url'].endswith('/token')]
            assert (await read(server)).contents[0].text == NORMAL
            assert provider._hermes_fence is None
            assert PRIVATE not in f.log()
            assert PRIVATE.encode().hex() not in json.dumps(f.snapshot())
            assert not isinstance(result, Exception), result
            assert result.contents[0].text == PRIVATE
            assert len(observed) == 1 and observed[0]['accepted'], observed
            assert all(observed[0]['policy'].get(k) == v for k, v in f.required_policy.items())
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('state', ['lazy', 'disconnected', 'ready'])
def test_actual_private_events_list_uses_config_only_without_public_mutation(compat_http, monkeypatch, state):
    from tools import mcp_tool_discovery as discovery, mcp_tool_events as events
    f = compat_http
    async def exercise():
        server = MCPServerTask('synthetic-events')
        cfg = config('http')
        with f.consent():await server.start(cfg)
        key = _server_key(server.name)
        monkeypatch.setattr(core, '_mcp_loop', asyncio.get_running_loop())
        if state != 'ready':await server.shutdown()
        if state == 'lazy':monkeypatch.setitem(core._lazy_server_configs, key, cfg)
        else:monkeypatch.setitem(core._servers, key, server)
        core._server_error_counts[key] = 2
        core._server_breaker_opened_at[key] = 123.0
        server._session_proven = False
        server._reconnect_retries = 2
        def public_state():
            return (dict(core._servers), dict(core._lazy_server_configs), dict(core._server_error_counts),
                    dict(core._server_breaker_opened_at), server._session_proven,
                    server._reconnect_retries, server._last_tool_call_at, server.session, server._ready.is_set())
        before = public_state()
        calls = []
        def forbidden(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError('private read woke/reconnected public carrier')
        handler = events._make_list_event_types_handler(server.name, 8)
        try:
            with monkeypatch.context() as guard:
                guard.setattr(discovery, '_ensure_lazy_server_connected', forbidden)
                guard.setattr(discovery, '_request_lazy_reconnect', forbidden)
                with incognito_scope(True):result = json.loads(await asyncio.to_thread(handler, {}))
            observed = public_state()
            assert calls == []
            assert observed == before, (before, observed)
            assert PRIVATE not in f.log() and PRIVATE.encode().hex() not in json.dumps(f.snapshot())
            if state != 'ready':
                server = MCPServerTask('synthetic-events')
                await server.start(cfg)
                core._lazy_server_configs.pop(key, None)
                core._servers[key] = server
            normal = json.loads(await asyncio.to_thread(handler, {}))
            assert normal.get('events') == [{'name': NORMAL}], normal
            assert result.get('events') == [{'name': PRIVATE}], result
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('op', ['subscribe', 'unsubscribe'])
def test_private_event_durable_controls_refuse_before_all_other_work(compat_http, monkeypatch, op):
    from tools import mcp_tool_discovery as discovery, mcp_tool_events as events
    f = compat_http
    def forbidden(*args, **kwargs):raise AssertionError('private durable operation crossed early refusal')
    for module, names in ((discovery, ['_get_connected_server_for_call']),
            (events, ['_subscribe_args_error', '_consent_error', '_load_routes', '_load_state', '_mutate_state', '_write_route'])):
        for name in names:monkeypatch.setattr(module, name, forbidden)
    before = f.snapshot()
    handler = getattr(events, '_make_' + op + '_event_handler')('not-configured', 8)
    with incognito_scope(True):result = json.loads(handler({'name': None, 'arguments': PRIVATE}))
    assert 'Incognito' in result['error'] and 'disabled' in result['error']
    assert f.snapshot() == before and f.records == [] and f.boundary == []
