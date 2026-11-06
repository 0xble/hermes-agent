"""Complete SDK/storage/transport audience lifecycle regressions (offline socket boundary).

Recreate the actual manager provider, not just its token Context. Discovery and
refresh selection are production code; only the network-facing transport is synthetic.
"""
import asyncio
from urllib.parse import parse_qs

import pytest

from agent.incognito import incognito_scope
from tools.mcp_tool import MCPServerTask
from tools.mcp_tool_callback_context import private_rpc_transport
from tools.mcp_oauth import _read_json, _write_json
from tests.tools.mcp_private_native_http import native_http, PRIVATE, NORMAL, URL, ISSUER
from tests.tools.test_mcp_private_compatibility import compat_http, expire, poison_private_provider
from tests.tools.test_mcp_private_oauth_transport import config, read


def provider(f):
    return next(iter(f.manager._entries.values())).provider


async def restart(f, server, cfg):
    old = provider(f)
    await server.shutdown()
    assert f.manager.evict(server.name) is not None
    replacement = MCPServerTask(server.name)
    await replacement.start(cfg)
    assert provider(f) is not old
    assert (await read(replacement)).contents[0].text == NORMAL
    return replacement


async def private_read(server):
    with incognito_scope(True):
        async with private_rpc_transport(server) as target:
            return await read(target)


async def lineage(f, server, audience):
    """One private rotation, followed by normal expiry and the next valid rotation."""
    p = provider(f)
    old = expire(p)
    metadata_before = p.context.storage._meta_path().read_bytes()
    try:result = await private_read(server)
    except Exception as exc:result = exc
    assert not isinstance(result, Exception), result
    assert result.contents[0].text == PRIVATE
    assert p.context.storage._meta_path().read_bytes() == metadata_before, 'private maintenance persisted discovery authority'
    saved = _read_json(p.context.storage._tokens_path())
    assert saved['refresh_token'] != old['refresh_token']
    private_refreshes = [r for r in f.boundary if r['kind'] == 'audience' and r['private']]
    assert len(private_refreshes) == 1
    assert private_refreshes[0]['accepted']
    assert private_refreshes[0]['params']['refresh_token'] == [old['refresh_token']]
    assert private_refreshes[0]['params'].get('resource') == ([audience] if audience else None)
    assert p._hermes_fence is None
    # The normal provider must reload the rotated generation, not replay its old grant.
    assert (await read(server)).contents[0].text == NORMAL
    expire(p)
    assert (await read(server)).contents[0].text == NORMAL
    latest = _read_json(p.context.storage._tokens_path())
    assert latest['refresh_token'] != saved['refresh_token']
    normal_refreshes = [r for r in f.boundary if r['kind'] == 'audience' and not r['private']]
    assert len(normal_refreshes) == 1
    assert normal_refreshes[0]['accepted']
    assert normal_refreshes[0]['params']['refresh_token'] == [saved['refresh_token']]
    assert normal_refreshes[0]['params'].get('resource') == ([audience] if audience else None)
    metadata = _read_json(p.context.storage._meta_path())
    assert 'hermes_resource' in metadata and metadata['hermes_resource'] == audience, 'normal refresh did not migrate grant policy'
    assert p._hermes_fence is None
    assert PRIVATE not in f.log()
    assert all(PRIVATE not in value for value in f.snapshot().values())


@pytest.mark.parametrize('kind,audience', [('http', ISSUER + '/'), ('sse', ISSUER + '/'), ('http', URL)])
def test_saved_audience_survives_restart_valid_normal_request_and_two_rotations(compat_http, kind, audience):
    f = compat_http
    f.required_resource = audience
    cfg = config(kind)
    async def exercise():
        server = MCPServerTask('synthetic-restart-lineage')
        with f.consent():await server.start(cfg)
        try:
            assert (await read(server)).contents[0].text == NORMAL
            path = provider(f).context.storage._meta_path()
            assert _read_json(path)['hermes_resource'] == audience
            server = await restart(f, server, cfg)
            assert _read_json(path).get('hermes_resource') == audience, 'valid normal reuse erased audience provenance'
            await lineage(f, server, audience)
            assert _read_json(path).get('hermes_resource') == audience
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('cold,poison', [(False, False), (False, True), (True, False), (True, True)])
def test_legacy_parent_grant_without_resource_is_supported_without_normal_refresh(compat_http, monkeypatch, cold, poison):
    f = compat_http
    f.required_resource = ISSUER + '/'
    cfg = config('http')
    async def exercise():
        server = MCPServerTask('synthetic-old-parent')
        with f.consent():await server.start(cfg)
        try:
            assert (await read(server)).contents[0].text == NORMAL
            path = provider(f).context.storage._meta_path()
            raw = _read_json(path)
            raw.pop('hermes_resource', None)
            _write_json(path, raw)
            if cold:server = await restart(f, server, cfg)
            if poison:poison_private_provider(f, monkeypatch)
            await lineage(f, server, f.required_resource)
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('legacy,cold', [(False, False), (False, True), (True, False), (True, True)])
def test_resource_free_policy_is_not_unknown_parent_audience(compat_http, legacy, cold):
    f = compat_http
    cfg = config('sse')
    cfg['headers'] = {'MCP-Protocol-Version': '2024-11-05'}
    original = f.respond
    async def resource_free(request):
        if '.well-known/oauth-protected-resource' in request.url.path:
            return f.http.Response(404)
        if request.url.path == '/token':
            params = parse_qs((await request.aread()).decode())
            if params.get('grant_type') == ['refresh_token'] and 'resource' in params:
                return f.http.Response(400, json={'error': 'invalid_target'})
        return await original(request)
    f.respond = resource_free
    async def exercise():
        server = MCPServerTask('synthetic-resource-free')
        with f.consent():await server.start(cfg)
        try:
            assert (await read(server)).contents[0].text == NORMAL
            path = provider(f).context.storage._meta_path()
            raw = _read_json(path)
            if legacy:
                raw.pop('hermes_resource', None)
                _write_json(path, raw)
            else:
                assert 'hermes_resource' in raw and raw['hermes_resource'] is None, 'known no-resource must differ from unavailable provenance'
            if cold:server = await restart(f, server, cfg)
            await lineage(f, server, None)
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('cold', [False, True])
def test_legacy_modern_no_prm_canonical_policy_keeps_normal_compatibility(compat_http, cold):
    f = compat_http
    cfg = config('http')
    original = f.respond
    async def no_prm(request):
        if '.well-known/oauth-protected-resource' in request.url.path:return f.http.Response(404)
        return await original(request)
    f.respond = no_prm
    async def exercise():
        server = MCPServerTask('synthetic-legacy-canonical')
        with f.consent():await server.start(cfg)
        try:
            assert (await read(server)).contents[0].text == NORMAL
            path = provider(f).context.storage._meta_path()
            raw = _read_json(path)
            raw.pop('hermes_resource', None)
            _write_json(path, raw)
            if cold:server = await restart(f, server, cfg)
            await lineage(f, server, URL)
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('fault', ['foreign-resource', 'wrong-issuer', 'discovery-issuer', 'discovery-resource', 'discovery-unavailable', 'discovery-malformed'])
def test_untrusted_or_unavailable_authority_never_refreshes_or_repairs_from_private_context(compat_http, monkeypatch, fault):
    f = compat_http
    f.required_resource = ISSUER + '/'
    cfg = config('http')
    async def exercise():
        server = MCPServerTask('synthetic-authority-control')
        with f.consent():await server.start(cfg)
        try:
            assert (await read(server)).contents[0].text == NORMAL
            p = provider(f)
            path = p.context.storage._meta_path()
            raw = _read_json(path)
            if fault == 'foreign-resource':raw['hermes_resource'] = 'https://foreign.synthetic.invalid/'
            elif fault == 'wrong-issuer':raw['issuer'] = 'https://foreign.synthetic.invalid/'
            else:raw.pop('hermes_resource', None)
            _write_json(path, raw)
            if fault.startswith('discovery-'):
                # A matching normal-owned parent is valid independent authority;
                # discovery refusal covers its genuinely absent/untrusted counterpart.
                p.context.protected_resource_metadata = None
                p._hermes_resource_policy = (False, None)
                p._hermes_resource_authority = None
                original = f.respond
                f.required_resource = None
                async def discovery_fault(request):
                    if '.well-known/oauth-protected-resource' in request.url.path:
                        if fault == 'discovery-unavailable':return f.http.Response(503)
                        if fault == 'discovery-malformed':return f.http.Response(200, json={'error': 'no-authority'})
                        return f.http.Response(200, json={
                            'resource': ('https://foreign.synthetic.invalid/' if fault == 'discovery-resource' else ISSUER + '/'),
                            'authorization_servers': ['https://foreign.synthetic.invalid/' if fault == 'discovery-issuer' else ISSUER]})
                    return await original(request)
                f.respond = discovery_fault
            expire(p)
            poison_private_provider(f, monkeypatch)
            before = f.snapshot()
            try:result = await private_read(server)
            except Exception as exc:result = exc
            assert isinstance(result, Exception), 'untrusted/unknown authority was accepted'
            after = f.snapshot()
            after.pop('mcp-tokens/synthetic-authority-control.json.refresh.lock', None)
            assert after == before
            assert not [r for r in f.boundary if r['kind'] == 'audience' and r['private']], 'untrusted/unknown audience reached token endpoint'
            assert p._hermes_fence is None
            assert PRIVATE not in f.log()
        finally:await server.shutdown()
    asyncio.run(exercise())
