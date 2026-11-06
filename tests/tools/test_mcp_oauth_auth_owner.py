"""Offline native SDK auth-owner provenance and ordinary direct-refresh contracts.

Only HTTP bytes are synthetic. Manager/provider/storage/SDK locks and RPC dispatch
are production; no private request is supplied to independent grant maintenance.
"""
import asyncio
from urllib.parse import parse_qs

import pytest

from agent.incognito import incognito_scope, is_incognito
from tools.mcp_oauth import _read_json, _write_json
from tools.mcp_tool import MCPServerTask
from tests.tools.mcp_private_native_http import native_http as _registered_native_http, PRIVATE, NORMAL, URL, ISSUER

native_http = _registered_native_http
from tests.tools.test_mcp_private_compatibility import compat_http as _registered_compat_http, expire, poison_private_provider

compat_http = _registered_compat_http
from tests.tools.test_mcp_oauth_audience_lifecycle import provider, restart, private_read, lineage
from tests.tools.test_mcp_private_oauth_transport import config, read

CHALLENGE = ISSUER + '/public-auth/resource-metadata'
PARENT = ISSUER + '/'


def challenge_only(f, *, available=True):
    """An ordinary SDK challenge, NOT a private-handshake authority hand-off."""
    original = f.respond
    f.prm_passthrough = True
    f.auth_requests = []
    f.auth_state = {'available': available, 'wellknown': 404, 'challenge': True}
    f.challenge_streams = []
    class HeaderOnlyChallenge(f.http.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            raise AssertionError('independent authorization challenge consumed an unbounded MCP body')
            yield b''
        async def aclose(self):self.closed = True
    async def respond(request):
        path = request.url.path
        if '.well-known/oauth-protected-resource' in path:
            f.auth_requests.append({'method': request.method, 'url': str(request.url),
                                    'headers': dict(request.headers), 'body': (await request.aread()).decode()})
            return f.http.Response(f.auth_state['wellknown'])
        if str(request.url) == CHALLENGE:
            f.auth_requests.append({'method': request.method, 'url': str(request.url),
                                    'headers': dict(request.headers), 'body': (await request.aread()).decode()})
            if not f.auth_state['available']:return f.http.Response(503)
            return f.http.Response(200, json={'resource': PARENT, 'authorization_servers': [ISSUER]})
        if str(request.url) == URL and not request.headers.get('authorization'):
            f.auth_requests.append({'method': request.method, 'url': str(request.url),
                                    'headers': dict(request.headers), 'body': (await request.aread()).decode()})
            if f.auth_state['challenge']:
                # Scope, error description and cookies must NOT become maintenance inputs.
                headers = {'www-authenticate':
                    'Bearer resource_metadata="' + CHALLENGE + '", scope="public-unused-scope"',
                    'set-cookie': 'public-unused-cookie=value'}
                if request.method == 'GET':
                    stream = HeaderOnlyChallenge()
                    f.challenge_streams.append(stream)
                    return f.http.Response(401, headers=headers, stream=stream)
                return f.http.Response(401, headers=headers)
            return f.http.Response(401)
        return await original(request)
    f.respond = respond
    f.required_resource = PARENT


def legacy(p):
    path = p.context.storage._meta_path()
    raw = _read_json(path)
    raw.pop('hermes_resource', None)
    _write_json(path, raw)
    return path.read_bytes()


@pytest.mark.parametrize('cold,poison,unavailable', [
    (False, False, True), (False, True, True), (True, False, False), (True, True, False)])
def test_challenge_only_legacy_parent_private_and_normal_lineage(compat_http, monkeypatch, cold, poison, unavailable):
    f = compat_http
    challenge_only(f)
    async def exercise():
        server = MCPServerTask('synthetic-challenge-parent')
        with f.consent():await server.start(config('http'))
        try:
            assert (await read(server)).contents[0].text == NORMAL
            p = provider(f)
            before = legacy(p)
            if cold:server = await restart(f, server, config('http'))
            assert provider(f).context.storage._meta_path().read_bytes() == before
            f.auth_state['available'] = not unavailable
            if poison:poison_private_provider(f, monkeypatch)
            f.auth_requests.clear()
            # No preceding ordinary refresh: lineage expires the untouched original grant.
            await lineage(f, server, PARENT)
            if unavailable:
                assert not f.auth_requests, 'matching warm authority should need no discovery'
            else:
                probes = [r for r in f.auth_requests if r['url'] == URL]
                assert probes and all(r['method'] == 'GET' and not r['body'] for r in probes)
                assert f.challenge_streams and all(s.closed for s in f.challenge_streams)
                assert all('authorization' not in r['headers'] and 'mcp-session-id' not in r['headers']
                           and 'mcp-protocol-version' not in r['headers'] for r in probes)
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('status', [404, 503])
def test_normal_warm_challenge_only_parent_refresh_needs_no_discovery(compat_http, status):
    f = compat_http
    challenge_only(f)
    async def exercise():
        server = MCPServerTask('synthetic-normal-warm-parent')
        with f.consent():await server.start(config('http'))
        try:
            assert (await read(server)).contents[0].text == NORMAL
            p = provider(f)
            legacy(p)
            old = expire(p)
            f.auth_state.update(available=False, wellknown=status)
            f.auth_requests.clear()
            assert (await read(server)).contents[0].text == NORMAL
            assert not f.auth_requests
            accepted = [r for r in f.boundary if r['kind'] == 'audience']
            assert len(accepted) == 1 and accepted[0]['accepted'] and not accepted[0]['private']
            assert accepted[0]['params']['resource'] == [PARENT]
            assert accepted[0]['params']['refresh_token'] == [old['refresh_token']]
            assert p._hermes_fence is None
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('fault', ['issuer', 'resource', 'malformed', 'unavailable', 'redirect'])
def test_cold_independent_challenge_authority_fails_closed(compat_http, monkeypatch, fault):
    f = compat_http
    challenge_only(f)
    async def exercise():
        server = MCPServerTask('synthetic-cold-challenge-refusal')
        with f.consent():await server.start(config('http'))
        try:
            assert (await read(server)).contents[0].text == NORMAL
            legacy(provider(f))
            server = await restart(f, server, config('http'))
            p = provider(f)
            expire(p)
            original = f.respond
            async def invalid(request):
                if str(request.url) == CHALLENGE:
                    if fault == 'unavailable':return f.http.Response(503)
                    if fault == 'redirect':return f.http.Response(302, headers={'location': ISSUER + '/elsewhere'})
                    if fault == 'malformed':return f.http.Response(200, json={'not': 'authority'})
                    return f.http.Response(200, json={
                        'resource': 'https://foreign.synthetic.invalid/' if fault == 'resource' else PARENT,
                        'authorization_servers': ['https://foreign.synthetic.invalid/' if fault == 'issuer' else ISSUER]})
                return await original(request)
            f.respond = invalid
            poison_private_provider(f, monkeypatch)
            before = f.snapshot()
            try:result = await private_read(server)
            except Exception as exc:result = exc
            assert isinstance(result, Exception), 'invalid independent challenge became grant authority'
            assert not [r for r in f.boundary if r['kind'] == 'audience' and r['private']]
            after = f.snapshot()
            for key in set(after) - set(before):
                assert key.endswith('.refresh.lock') and after[key] == ''
                after.pop(key)
            assert after == before
            assert p._hermes_fence is None
            assert PRIVATE not in f.log()
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('after_receipt', [False, True])
@pytest.mark.parametrize('fault', ['home', 'server', 'issuer', 'client', 'registration', 'grant', 'resource', 'policy', 'evicted',
                                   'reloaded-grant', 'disk-grant', 'disk-registration', 'disk-issuer', 'disk-endpoint', 'endpoint', 'disk-resource'])
def test_stale_or_mismatched_normal_owner_cannot_supply_private_authority(compat_http, monkeypatch, fault, after_receipt):
    f = compat_http
    challenge_only(f)
    async def exercise():
        server = MCPServerTask('synthetic-owner-provenance')
        with f.consent():await server.start(config('http'))
        try:
            assert (await read(server)).contents[0].text == NORMAL
            p = provider(f)
            legacy(p)
            expire(p)
            if after_receipt:
                assert (await private_read(server)).contents[0].text == PRIVATE
                saved_receipt = _read_json(p.context.storage._tokens_path())
                assert saved_receipt is not None
                assert p.context.current_tokens.refresh_token != saved_receipt['refresh_token']
                expire(p)
                f.boundary.clear()
            # All public discovery is independently unavailable, making accidental
            # adoption of the normal provider's stale parent observable at /token.
            f.auth_state.update(available=False, wellknown=503)
            async def fault_home():
                p.context.storage._hermes_home = f.home / 'other-home'

            async def fault_server():
                p._hermes_configured_server_url = ISSUER + '/other'

            async def fault_issuer():
                p.context.oauth_metadata = p.context.oauth_metadata.model_copy(update={'issuer': 'https://foreign.synthetic.invalid/'})

            async def fault_client():
                p.context.client_info = p.context.client_info.model_copy(update={'client_id': 'other-client'})

            async def fault_registration():
                p.context.client_info = p.context.client_info.model_copy(update={'client_secret': 'other-registration'})

            async def fault_grant():
                p.context.current_tokens = p.context.current_tokens.model_copy(update={'refresh_token': 'other-grant'})

            async def fault_resource():
                p.context.protected_resource_metadata = p.context.protected_resource_metadata.model_copy(update={'resource': ISSUER + '/other'})

            async def fault_policy():
                p._hermes_resource_policy = (True, ISSUER + '/other')

            async def fault_evicted():
                assert f.manager.evict(server.name)

            async def fault_reloaded_grant():
                path = p.context.storage._tokens_path()
                raw = _read_json(path)
                raw['refresh_token'] = 'independently-replaced-grant'
                _write_json(path, raw)
                if fault == 'reloaded-grant':await p._initialize()

            async def fault_endpoint():
                p.context.oauth_metadata = p.context.oauth_metadata.model_copy(update={'token_endpoint': ISSUER + '/other-token'})

            async def fault_disk_resource():
                path = p.context.storage._meta_path()
                raw = _read_json(path)
                raw['hermes_resource'] = 'https://foreign.synthetic.invalid/'
                _write_json(path, raw)

            async def fault_disk_registration():
                path = p.context.storage._client_info_path()
                raw = _read_json(path)
                raw['client_secret'] = 'independently-replaced-registration'
                raw['token_endpoint_auth_method'] = 'client_secret_post'
                _write_json(path, raw)

            async def fault_disk_issuer():
                path = p.context.storage._meta_path()
                raw = _read_json(path)
                raw['issuer' if fault == 'disk-issuer' else 'token_endpoint'] = 'https://foreign.synthetic.invalid/'
                _write_json(path, raw)

            faults = {
                'home': fault_home,
                'server': fault_server,
                'issuer': fault_issuer,
                'client': fault_client,
                'registration': fault_registration,
                'grant': fault_grant,
                'resource': fault_resource,
                'policy': fault_policy,
                'evicted': fault_evicted,
                'reloaded-grant': fault_reloaded_grant,
                'disk-grant': fault_reloaded_grant,
                'endpoint': fault_endpoint,
                'disk-resource': fault_disk_resource,
                'disk-registration': fault_disk_registration,
                'disk-issuer': fault_disk_issuer,
                'disk-endpoint': fault_disk_issuer,
            }
            await faults[fault]()
            poison_private_provider(f, monkeypatch)
            before = f.snapshot()
            try:result = await private_read(server)
            except Exception as exc:result = exc
            assert isinstance(result, Exception), 'mismatched owner supplied authority'
            assert not [r for r in f.boundary if r['kind'] == 'audience' and r['private']]
            after = f.snapshot()
            for key in set(after) - set(before):
                assert key.endswith('.refresh.lock') and after[key] == ''
                after.pop(key)
            assert after == before
            assert p._hermes_fence is None
            assert PRIVATE not in f.log()
        finally:await server.shutdown()
    asyncio.run(exercise())


def test_matching_owner_is_not_a_stale_snapshot_during_normal_continuation(compat_http):
    f = compat_http
    challenge_only(f)
    async def exercise():
        server = MCPServerTask('synthetic-concurrent-owner')
        with f.consent():await server.start(config('http'))
        try:
            assert (await read(server)).contents[0].text == NORMAL
            p = provider(f)
            legacy(p)
            old = expire(p)
            f.auth_state.update(available=False, wellknown=503)
            # Deterministically let private renewal hold the real fence before
            # normal continuation starts; no timing sleep or mocked lock.
            entered, release = asyncio.Event(), asyncio.Event()
            original = f.respond
            async def hold_refresh(request):
                if request.url.path == '/token' and b'grant_type=refresh_token' in await request.aread() and is_incognito():
                    entered.set()
                    await release.wait()
                return await original(request)
            f.respond = hold_refresh
            private_task = asyncio.create_task(private_read(server))
            waiter = asyncio.create_task(entered.wait())
            try:
                await asyncio.wait((waiter, private_task), timeout=10, return_when=asyncio.FIRST_COMPLETED)
                error = private_task.exception() if private_task.done() else None
                assert entered.is_set(), ('private refresh did not reach the accepting token endpoint', error)
                normal_task = asyncio.create_task(read(server))
                release.set()
                private, normal = await asyncio.gather(private_task, normal_task)
            finally:
                release.set()
                waiter.cancel()
                if not private_task.done():private_task.cancel()
                await asyncio.gather(waiter, private_task, return_exceptions=True)
            assert private.contents[0].text == PRIVATE
            assert normal.contents[0].text == NORMAL
            accepted = [r for r in f.boundary if r['kind'] == 'audience']
            assert len(accepted) == 1 and accepted[0]['accepted']
            assert accepted[0]['params']['refresh_token'] == [old['refresh_token']]
            assert p._hermes_fence is None
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('fault', [503, 429, 401, 403, 'redirect', 'invalid', 'dns'])
def test_normal_saved_asm_unknown_audience_keeps_direct_sdk_refresh(compat_http, fault):
    f = compat_http
    f.required_resource = URL
    f.prm_passthrough = True
    async def exercise():
        server = MCPServerTask('synthetic-normal-direct')
        with f.consent():await server.start(config('http'))
        try:
            assert (await read(server)).contents[0].text == NORMAL
            legacy(provider(f))
            server = await restart(f, server, config('http'))
            p = provider(f)
            assert p.context.oauth_metadata is not None
            assert p.context.protected_resource_metadata is None
            assert p.context.storage.load_oauth_resource_policy() == (False, None)
            original = f.respond
            attempts = []
            async def unavailable(request):
                if '.well-known/oauth-protected-resource' in request.url.path:
                    attempts.append(str(request.url))
                    if fault == 'dns':raise f.http.ConnectError('synthetic DNS unavailable', request=request)
                    if fault == 'redirect':return f.http.Response(302, headers={'location': ISSUER + '/not-authority'})
                    if fault == 'invalid':return f.http.Response(200, json={'not': 'metadata'})
                    return f.http.Response(fault)
                return await original(request)
            f.respond = unavailable
            old = expire(p)
            try:result = await read(server)
            except Exception as exc:result = exc
            assert not isinstance(result, Exception), result
            assert result.contents[0].text == NORMAL
            assert not attempts, 'normal direct refresh acquired an unrelated discovery prerequisite'
            accepted = [r for r in f.boundary if r['kind'] == 'audience']
            assert len(accepted) == 1 and accepted[0]['accepted']
            assert accepted[0]['params']['refresh_token'] == [old['refresh_token']]
            assert accepted[0]['params']['resource'] == [URL]
            saved = _read_json(p.context.storage._meta_path())
            assert saved['hermes_resource'] == URL
            assert p._hermes_fence is None
        finally:await server.shutdown()
    asyncio.run(exercise())


def test_missing_asm_initialization_still_discovers_before_refresh(compat_http):
    f = compat_http
    async def exercise():
        server = MCPServerTask('synthetic-missing-asm-control')
        with f.consent():await server.start(config('http'))
        assert (await read(server)).contents[0].text == NORMAL
        p = provider(f)
        expire(p)
        p.context.storage._meta_path().unlink()
        await server.shutdown()
        assert f.manager.evict(server.name)
        original = f.respond
        attempts = []
        async def unavailable(request):
            if '.well-known/oauth-protected-resource' in request.url.path:
                attempts.append(str(request.url))
                return f.http.Response(404)
            return await original(request)
        f.respond = unavailable
        replacement = MCPServerTask(server.name)
        try:
            await replacement.start(config('http'))
            assert (await read(replacement)).contents[0].text == NORMAL
            assert attempts, 'missing ASM prefetch was skipped'
            traffic = [r for r in f.boundary if r['kind'] == 'transport']
            prm_index = next(i for i,r in enumerate(traffic) if '.well-known/oauth-protected-resource/mcp' in r['url'])
            token_index = next(i for i,r in enumerate(traffic) if i > prm_index and r['url'].endswith('/token'))
            assert prm_index < token_index
            assert any(r['kind'] == 'audience' and r['accepted'] for r in f.boundary)
        finally:await replacement.shutdown()
    asyncio.run(exercise())

@pytest.mark.parametrize('cold,poison,recovery', [
    (False, False, False), (False, True, False), (True, False, False), (True, True, False),
    (False, True, True)])
def test_uninterrupted_private_receipts_advance_without_normal_token_context(compat_http, monkeypatch, cold, poison, recovery):
    f = compat_http
    challenge_only(f)
    async def exercise():
        server = MCPServerTask('synthetic-uninterrupted-owner')
        with f.consent():await server.start(config('http'))
        try:
            assert (await read(server)).contents[0].text == NORMAL
            metadata = legacy(provider(f))
            if cold:server = await restart(f, server, config('http'))
            p = provider(f)
            original = expire(p)
            owner_tokens = p.context.current_tokens
            owner_snapshot = owner_tokens.model_dump(mode='json')
            client = p.context.storage._client_info_path().read_bytes()
            token_name = str(p.context.storage._tokens_path().relative_to(f.home))
            stable = {n:v for n,v in f.snapshot().items() if n != token_name and not n.endswith('.refresh.lock')}
            normal_traffic = [r for r in f.records if not r['private']]
            f.auth_requests.clear()
            if poison:poison_private_provider(f, monkeypatch)
            assert original is not None
            grant = original['refresh_token']
            generations = [grant]
            for generation in range(3):
                # Disk expiry only. In particular, NEVER load the saved replacement
                # into the normal SDK Context between private renewals.
                raw = _read_json(p.context.storage._tokens_path())
                assert raw is not None
                assert raw['refresh_token'] == grant
                raw['expires_at'] = 1
                _write_json(p.context.storage._tokens_path(), raw)
                if generation or not cold:
                    f.auth_state.update(available=bool(recovery and generation == 1),
                                        wellknown=404 if recovery and generation == 1 else 503)
                attempts = len(f.auth_requests)
                try:result = await private_read(server)
                except Exception as exc:result = exc
                assert not isinstance(result, Exception), ('uninterrupted private generation failed', generation)
                assert result.contents[0].text == PRIVATE
                assert p.context.current_tokens is owner_tokens
                assert p.context.current_tokens.model_dump(mode='json') == owner_snapshot
                assert [r for r in f.records if not r['private']] == normal_traffic
                assert p.context.storage._meta_path().read_bytes() == metadata
                assert p.context.storage._client_info_path().read_bytes() == client
                if generation == 0 and cold:
                    probes = [r for r in f.auth_requests if r['url'] == URL]
                    assert probes and all(r['method'] == 'GET' and not r['body'] for r in probes)
                    assert all('authorization' not in r['headers'] and 'mcp-session-id' not in r['headers'] for r in probes)
                    assert all(s.closed for s in f.challenge_streams)
                elif not recovery or generation != 1:
                    observed_attempts = len(f.auth_requests)
                    assert observed_attempts == attempts, 'own replacement lineage lost independent authority'
                rotations = [r for r in f.boundary if r['kind'] == 'audience']
                assert len(rotations) == generation + 1
                assert rotations[-1]['accepted'] and rotations[-1]['private']
                assert rotations[-1]['params']['refresh_token'] == [grant]
                assert rotations[-1]['params']['resource'] == [PARENT]
                assert PRIVATE not in str(rotations[-1]['params'])
                saved = _read_json(p.context.storage._tokens_path())
                assert saved is not None
                grant = saved['refresh_token']
                assert grant not in generations
                generations.append(grant)
                assert p._hermes_fence is None
                assert PRIVATE not in f.log()
                assert all(PRIVATE not in bytes.fromhex(v).decode(errors='replace') for v in f.snapshot().values())
            assert stable == {n:v for n,v in f.snapshot().items() if n != token_name and not n.endswith('.refresh.lock')}
            # Ordinary continuation must read the latest saved generation WITHOUT
            # consuming it, then refresh that generation exactly once at expiry.
            f.auth_state.update(available=False, wellknown=503)
            probes = len(f.auth_requests)
            assert (await read(server)).contents[0].text == NORMAL
            assert len([r for r in f.boundary if r['kind'] == 'audience']) == 3
            expire(p)
            assert (await read(server)).contents[0].text == NORMAL
            normal = [r for r in f.boundary if r['kind'] == 'audience' and not r['private']]
            assert len(normal) == 1 and normal[0]['accepted']
            assert normal[0]['params']['refresh_token'] == [grant]
            assert normal[0]['params']['resource'] == [PARENT]
            assert len(f.auth_requests) == probes
            final_meta = _read_json(p.context.storage._meta_path())
            assert final_meta is not None and final_meta['hermes_resource'] == PARENT
            assert p._hermes_fence is None
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('private_first', [False, True])
def test_later_receipt_serializes_private_and_normal_first_continuation(compat_http, private_first):
    f = compat_http
    challenge_only(f)
    async def exercise():
        server = MCPServerTask('synthetic-later-receipt-race')
        with f.consent():await server.start(config('http'))
        try:
            assert (await read(server)).contents[0].text == NORMAL
            p = provider(f)
            legacy(p)
            owner_grant = p.context.current_tokens.refresh_token
            f.auth_requests.clear()
            f.auth_state.update(available=False, wellknown=503)
            for generation in range(2):
                expire(p)
                try:result = await private_read(server)
                except Exception as exc:result = exc
                assert not isinstance(result, Exception), ('receipt setup generation failed', generation)
                assert result.contents[0].text == PRIVATE
                assert p.context.current_tokens.refresh_token == owner_grant
            old = expire(p)
            entered, release = asyncio.Event(), asyncio.Event()
            original = f.respond
            async def hold_refresh(request):
                if (request.url.path == '/token' and b'grant_type=refresh_token' in await request.aread()
                        and is_incognito() == private_first):
                    entered.set()
                    await release.wait()
                return await original(request)
            f.respond = hold_refresh
            first = asyncio.create_task(private_read(server) if private_first else read(server))
            waiter = asyncio.create_task(entered.wait())
            second = None
            try:
                await asyncio.wait((waiter, first), timeout=10, return_when=asyncio.FIRST_COMPLETED)
                assert entered.is_set(), 'first contender did not acquire the saved grant fence'
                second = asyncio.create_task(read(server) if private_first else private_read(server))
                release.set()
                results = await asyncio.gather(first, second)
            finally:
                release.set()
                pending = [t for t in (waiter, first, second) if t is not None]
                for t in pending:
                    if not t.done():t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            assert [r.contents[0].text for r in results] == ([PRIVATE, NORMAL] if private_first else [NORMAL, PRIVATE])
            rotations = [r for r in f.boundary if r['kind'] == 'audience']
            assert len(rotations) == 3 and all(r['accepted'] for r in rotations)
            assert old is not None
            assert rotations[-1]['params']['refresh_token'] == [old['refresh_token']]
            assert rotations[-1]['private'] == private_first
            auth_attempts = len(f.auth_requests)
            assert auth_attempts == 0
            assert p._hermes_fence is None and PRIVATE not in f.log()
        finally:await server.shutdown()
    asyncio.run(exercise())
