"""Real installed SDK HTTP/SSE, OAuth discovery/DCR/code exchange and disk sinks."""
import asyncio
import json
from pathlib import Path

import pytest

from agent.incognito import incognito_scope, is_incognito
from tools.mcp_tool import MCPServerTask
from tools.mcp_tool_callback_context import private_rpc_transport
from tests.tools.mcp_private_native_http import native_http, PRIVATE, NORMAL, URL


def config(kind):
    return {'url': URL, 'transport': kind, 'auth': 'oauth', 'oauth': {'cimd': False, 'redirect_port': 45678},
            'protocol': 'legacy', 'connect_timeout': 3}


async def read(server, marker=None):
    marker = marker or ('fixture://' + (PRIVATE if is_incognito() else NORMAL))
    return await asyncio.wait_for(server.session.read_resource(marker), 3)


@pytest.mark.parametrize('kind', ['http', 'sse'])
def test_private_native_oauth_flow_has_no_disk_or_shared_provider_mutation(native_http, kind):
    f = native_http
    async def exercise():
        server = MCPServerTask('synthetic-native-oauth')
        server._config = config(kind)
        before = f.snapshot()
        with f.consent(), incognito_scope(True):
            async with private_rpc_transport(server) as private:
                result = await read(private)
                assert result.contents[0].text == PRIVATE
                # Force real SDK refresh on this independently minted private grant.
                provider = f.providers[-1]
                provider.context.token_expiry_time = -1
                assert (await read(private)).contents[0].text == PRIVATE
                assert any('grant_type=refresh_token' in r['body'] for r in f.records)
        after = f.snapshot()
        assert after == before, 'private actual OAuth discovery/DCR/exchange wrote owned auth files'
        assert not f.manager._entries, 'private provider escaped into shared normal cache'
        assert PRIVATE not in f.log(), 'private HTTP/SSE ingress escaped to owned FileHandler'
        # A fresh normal connection must persist and work after the private lifetime ended.
        with f.consent():
            await server.start(config(kind))
        try:
            result = await read(server)
            assert result.contents[0].text == NORMAL
        finally:await server.shutdown()
        assert f.snapshot() != before
        assert NORMAL in json.dumps(f.snapshot()) or any(NORMAL.encode().hex() in v for v in f.snapshot().values())
        assert NORMAL in f.log(), 'normal transport diagnostics did not resume'
    asyncio.run(exercise())


@pytest.mark.parametrize('kind', ['http', 'sse'])
@pytest.mark.parametrize('case', ['valid', 'malformed', 'cancel'])
def test_warm_normal_oauth_reads_private_one_shot_then_normal_resume(native_http, kind, case):
    f = native_http
    async def exercise():
        server = MCPServerTask('synthetic-warm-oauth')
        with f.consent():await server.start(config(kind))
        try:
            normal_provider = next(iter(f.manager._entries.values())).provider
            before = f.snapshot()
            f.current.case = 'hold' if case == 'cancel' else case
            with incognito_scope(True):
                async with private_rpc_transport(server) as private:
                    if case == 'cancel':
                        call = asyncio.create_task(read(private))
                        await asyncio.wait_for(f.current.entered.wait(), 3)
                        call.cancel()
                        with pytest.raises(asyncio.CancelledError):await call
                        f.current.hold.set()
                    else:
                        try:await read(private)
                        except Exception:
                            if case != 'malformed':raise
            assert f.snapshot() == before, 'private lifetime modified warm normal auth files'
            assert f.providers[-1] is not normal_provider, 'private transport reused mutable normal OAuth provider'
            assert PRIVATE not in f.log(), 'private malformed ingress persisted'
            f.current.case = 'valid'
            result = await read(server)
            assert result.contents[0].text == NORMAL
            assert next(iter(f.manager._entries.values())).provider is normal_provider
            assert NORMAL in f.log()
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('kind, readers', [('http', 1), ('sse', 1), ('http', 2)])
def test_saved_rotating_grant_supports_private_read_and_normal_refresh_resumes(native_http, kind, readers):
    f = native_http
    async def exercise():
        server = MCPServerTask('synthetic-saved-rotation')
        with f.consent():await server.start(config(kind))
        try:
            provider = next(iter(f.manager._entries.values())).provider
            assert (await read(server)).contents[0].text == NORMAL
            # Persist a synthetic expired generation using normal storage (not the private core).
            from tools.mcp_oauth import _read_json, _write_json
            path = provider.context.storage._tokens_path()
            raw = _read_json(path)
            assert raw is not None
            raw['expires_at'] = 1
            _write_json(path, raw)
            before = f.snapshot()
            token_posts = sum('/token' in r['url'] for r in f.records)
            async def private_read():
                with incognito_scope(True):
                    try:
                        async with private_rpc_transport(server) as private:return await read(private)
                    except Exception as exc:return exc
            private_results = await asyncio.gather(*(private_read() for _ in range(readers)))
            assert all(not isinstance(r, Exception) for r in private_results), private_results
            assert all(r.contents[0].text == PRIVATE for r in private_results)
            assert sum('/token' in r['url'] for r in f.records) == token_posts + 1
            renewed = _read_json(path)
            assert renewed is not None
            assert renewed['refresh_token'] != raw['refresh_token'], 'single-use grant rotation not durably retained'
            assert PRIVATE.encode().hex() not in json.dumps(f.snapshot()), 'private read content in auth/home sinks'
            assert provider.context.current_tokens.refresh_token == raw['refresh_token'], 'private state mutated public provider'
            token_posts += 1
            assert PRIVATE not in f.log()
            # Normal refresh still owns its original grant and persists rotation, with one provider lock.
            renewed['expires_at'] = 1
            _write_json(path, renewed)
            provider.context.token_expiry_time = -1
            f.current.case = 'valid'
            async with f.http.AsyncClient(auth=provider) as client:
                payload = {'jsonrpc': '2.0', 'id': 999, 'method': 'resources/list'}
                results = await asyncio.wait_for(asyncio.gather(
                    client.post(URL, json=payload), client.post(URL, json=payload)), 5)
                assert all(r.status_code == 200 for r in results)
            assert sum('/token' in r['url'] for r in f.records) == token_posts + 1
            assert f.snapshot() != before
            assert provider._hermes_fence is None
            assert (await read(server)).contents[0].text == NORMAL
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('operation', ['client-fixup', 'preregister', 'metadata-issuer', 'cimd-poison-restore'])
def test_private_native_provider_storage_lifetime_outlasts_context_without_disk_write(native_http, operation):
    f = native_http
    async def exercise():
        server = MCPServerTask('synthetic-storage-owner')
        with f.consent():await server.start(config('http'))
        try:
            normal = next(iter(f.manager._entries.values())).provider
            before = f.snapshot()
            with incognito_scope(True):
                private = f.manager.get_or_build_provider(server.name, URL, config('http')['oauth'])
                await private._initialize()  # Native SDK initialization/client fixup and issuer binding.
            # Late provider work must remain private after the caller Context has ended.
            storage = private.context.storage
            if operation == 'client-fixup':
                info = private.context.client_info.model_copy(update={'client_id': PRIVATE, 'client_secret': PRIVATE, 'token_endpoint_auth_method': None})
                await storage.set_client_info(info)
                assert (await storage.get_client_info()).token_endpoint_auth_method == 'client_secret_post'
            elif operation == 'preregister':
                from tools.mcp_oauth import _maybe_preregister_client
                cfg = {'client_id': PRIVATE, '_resolved_port': 45678}
                _maybe_preregister_client(storage, cfg, private.context.client_metadata)
            elif operation == 'metadata-issuer':
                meta_type = type(private.context.oauth_metadata)
                raw_meta = private.context.oauth_metadata.model_dump(mode='json')
                raw_meta['issuer'] = 'https://synthetic-private-transport.invalid'
                meta = meta_type.model_validate(raw_meta)
                storage.save_oauth_metadata(meta)
                storage.stamp_issuer(PRIVATE)
                storage.strip_refresh_token()
            else:
                snap = storage.snapshot()
                storage.mark_cimd_rejected()
                assert storage.cimd_rejected()
                storage.poison_client_registration()
                storage.remove()
                storage.restore(snap)
            assert f.snapshot() == before, 'late private client/metadata/CIMD/poison write persisted'
            assert PRIVATE not in f.log(), 'late volatile storage operation logged private content'
            assert next(iter(f.manager._entries.values())).provider is normal
            assert PRIVATE not in json.dumps(normal.context.client_info.model_dump(mode='json'))
            assert (await read(server)).contents[0].text == NORMAL
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('kind', ['http', 'sse'])
def test_native_private_invalid_client_and_cimd_fallback_stay_volatile(native_http, kind):
    f = native_http
    async def exercise():
        server = MCPServerTask('synthetic-native-poison')
        server._config = config(kind)
        before = f.snapshot()
        with f.consent(), incognito_scope(True):
            async with private_rpc_transport(server) as private:
                await read(private)
                provider = f.providers[-1]
                # Synthetic consent server has accepted this client-metadata identity;
                # token rejection below traverses the real provider's CIMD + poison code.
                provider.context.client_metadata_url = provider.context.client_info.client_id
                provider.context.token_expiry_time = -1
                f.current.case = 'reject-refresh'
                try:await read(private)
                except Exception:pass
                assert any(r['url'].endswith('/token') and 'grant_type=refresh_token' in r['body'] for r in f.records)
                assert provider.context.storage.cimd_rejected()
                assert provider.context.client_metadata_url is None
        assert f.snapshot() == before, 'native invalid_client/CIMD fallback wrote private state'
        assert PRIVATE not in f.log()
        f.current.case = 'valid'
        with f.consent():await server.start(config(kind))
        try:assert (await read(server)).contents[0].text == NORMAL
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('kind', ['http', 'sse'])
def test_private_factory_failure_does_not_recover_or_contaminate_public_session(native_http, monkeypatch, kind):
    f = native_http
    async def exercise():
        from tools import mcp_tool as core
        from tools.mcp_tool_handlers import _make_read_resource_handler
        from tools.mcp_tool_scope import _server_key
        server = MCPServerTask('synthetic-factory-recovery')
        monkeypatch.setattr(core, '_mcp_loop', asyncio.get_running_loop())
        monkeypatch.setitem(core._servers, _server_key(server.name), server)
        with f.consent():await server.start(config(kind))
        try:
            original_session = server.session
            assert (await read(server)).contents[0].text == NORMAL
            before = f.snapshot()
            normal_initializes = sum(not r['private'] and 'initialize' in r['body'] for r in f.records)
            errors_before = dict(core._server_error_counts)
            f.current.case = 'expired'
            with incognito_scope(True):
                result = await asyncio.wait_for(asyncio.to_thread(
                    _make_read_resource_handler(server.name, 5), {'uri': 'fixture://' + PRIVATE}), 30)
            assert 'error' in json.loads(result)
            assert core._server_error_counts == errors_before, 'private failure charged the normal circuit breaker'
            assert server.session is original_session, 'private failure recovered/replaced unrelated normal session'
            assert sum(not r['private'] and 'initialize' in r['body'] for r in f.records) == normal_initializes
            assert f.snapshot() == before
            assert PRIVATE not in f.log()
            f.current.case = 'valid'
            assert (await read(server)).contents[0].text == NORMAL
        finally:await server.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('case', ['private-context', 'unsafe-saved'])
def test_saved_auth_maintenance_uses_only_saved_inputs_and_fails_closed(native_http, case):
    f = native_http
    async def exercise():
        from tools.mcp_oauth import _read_json, _write_json
        from mcp.shared.auth import ProtectedResourceMetadata
        server = MCPServerTask('synthetic-maintenance-provenance')
        with f.consent():await server.start(config('http'))
        normal = next(iter(f.manager._entries.values())).provider
        storage = normal.context.storage
        token_path = storage._tokens_path()
        meta_path = storage._meta_path()
        saved_meta = _read_json(meta_path)
        assert saved_meta is not None
        try:
            with incognito_scope(True):
                async with private_rpc_transport(server) as private:
                    provider = f.providers[-1]
                    provider.context.client_metadata.scope = PRIVATE
                    private_meta = provider.context.oauth_metadata.model_dump(mode='json')
                    private_meta['token_endpoint'] = URL + '/' + PRIVATE
                    provider.context.oauth_metadata = type(provider.context.oauth_metadata).model_validate(private_meta)
                    provider.context.protected_resource_metadata = ProtectedResourceMetadata(resource=URL, authorization_servers=[URL + '/' + PRIVATE])
                    provider.context.token_expiry_time = -1
                    raw = _read_json(token_path)
                    assert raw is not None
                    raw['expires_at'] = 1
                    _write_json(token_path, raw)
                    if case == 'unsafe-saved':
                        _write_json(meta_path, dict(saved_meta, issuer=URL + '/wrong-issuer'))
                    posts = len([r for r in f.records if '/token' in r['url']])
                    before = token_path.read_bytes()
                    answer, error = None, None
                    try:answer = await read(private)
                    except Exception as exc:error = exc
            after_private = token_path.read_bytes()
            later_posts = [r for r in f.records if '/token' in r['url']][posts:]
            # Normal read works even when the private renewal was deliberately refused.
            assert (await read(server)).contents[0].text == NORMAL
            if case == 'unsafe-saved':
                assert error is not None
                assert not later_posts
                assert after_private == before
            else:
                assert error is None, repr(error)
                assert answer is not None
                assert answer.contents[0].text == PRIVATE
                assert len(later_posts) == 1
                assert later_posts[0]['url'] == 'https://mcp.synthetic.invalid/token'
                assert PRIVATE not in later_posts[0]['body']
                assert PRIVATE.encode().hex() not in json.dumps(f.snapshot())
            assert PRIVATE not in f.log()
        finally:
            _write_json(meta_path, saved_meta)
            await server.shutdown()
    asyncio.run(exercise())
