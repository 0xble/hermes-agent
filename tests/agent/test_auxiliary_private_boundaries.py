"""Real-import privacy regressions: only wire I/O and consent answers are synthetic.

Config, provider resolution, credential pools, SDK requests, callback ownership and
Events persistence run as shipped. No AST/source-body substitutes are used here.
"""
import asyncio
import contextvars
import json
import logging
import os
import threading

import openai
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace as NS

import httpx
import pytest
import hermes_yaml as yaml
from openai import OpenAI, AsyncOpenAI

from agent import auxiliary_client as aux
from agent.incognito import incognito_scope, is_incognito, register_session, set_private_route
from tools import mcp_tool_handlers as handlers, mcp_tool_events as events
from tools.mcp_tool import MCPServerTask
from tests.tools.mcp_private_wire import offline_mcp
from tools.mcp_tool_sampling import SamplingHandler

SECRET = 'SYNTHETIC_PRIVATE_PAYLOAD_6732'
POOL_KEY = 'synthetic-owned-pool-key'
OWN_URL = 'https://owned.fixture.invalid/v1'
FOREIGN_URL = 'https://foreign.fixture.invalid/v1'


@pytest.fixture
def owned(tmp_path, monkeypatch):
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_DISABLE_MODEL_PREFETCH', '1')
    import platform
    monkeypatch.setattr(platform, 'platform', lambda: 'synthetic-offline-platform')
    from agent import models_dev
    import time
    monkeypatch.setattr(models_dev, '_models_dev_cache', {'synthetic-offline-catalog': {'models': {}}})
    monkeypatch.setattr(models_dev, '_models_dev_cache_time', time.time())
    cfg = {
        'model': {'provider': 'custom:private-slug', 'model': 'fixture-model', 'base_url': OWN_URL},
        'providers': {
            'private-slug': {'base_url': OWN_URL, 'api_mode': 'chat_completions',
                             'extra_headers': {'X-Owned-Route': 'synthetic-owned-header'}},
            'own-alias': {'base_url': FOREIGN_URL, 'api_mode': 'chat_completions',
                          'api_key': 'synthetic-alias-key', 'extra_headers': {'X-Alias': 'yes'}},
        },
        'auxiliary': {'title_generation': {'provider': 'custom:private-slug', 'model': 'fixture-model',
                                          'api_mode': 'chat_completions'}},
        'platforms': {'webhook': {'enabled': True, 'extra': {'public_url': 'https://webhook.fixture.invalid'}}},
    }
    (home / 'config.yaml').write_text(yaml.safe_dump(cfg))
    def seed(key=POOL_KEY):
        from hermes_cli.auth import _save_auth_store
        _save_auth_store({'version': 1, 'credential_pool': {'private-slug': [{
            'id': 'synthetic-entry', 'auth_type': 'api_key', 'access_token': key,
            'priority': 0, 'source': 'manual', 'label': 'synthetic-fixture', 'created_at': '2026-01-01T00:00:00Z',
        }]}})
    seed()
    aux._client_cache.clear()
    requests = []
    clients = []
    fail = []
    def wire(request):
        requests.append(request)
        if fail:
            return httpx.Response(400, json={'error': {'message': SECRET, 'type': 'invalid_request_error', 'code': 'fixture'}}, request=request)
        return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 0,
            'model': 'fixture-model', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}, request=request)
    class SyncWire(OpenAI):
        def __init__(self, **kw):
            old = kw.pop('http_client', None)
            if old is not None:old.close()
            super().__init__(**kw, http_client=httpx.Client(transport=httpx.MockTransport(wire)))
            clients.append(self)
    class AsyncWire(AsyncOpenAI):
        def __init__(self, **kw):
            kw.pop('http_client', None)
            super().__init__(**kw, http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire)))
            clients.append(self)
    monkeypatch.setattr(aux, 'OpenAI', SyncWire)
    monkeypatch.setattr(openai, 'AsyncOpenAI', AsyncWire)
    yield NS(home=home, cfg=cfg, requests=requests, seed=seed, fail=fail)
    aux._client_cache.clear()
    for client in clients:
        if isinstance(client, OpenAI):client.close()
        else:asyncio.run(client.close())


@contextmanager
def named_private_route(provider='custom:private-slug', base_url=OWN_URL):
    sid = 'synthetic-aux-boundary-session'
    register_session(sid)
    set_private_route((sid,), {'provider': provider, 'model': 'fixture-model',
                              'base_url': base_url, 'api_mode': 'chat_completions'})
    try:
        with incognito_scope(True, sid):
            yield
    finally:
        register_session(sid, False)


def invoke(async_mode, **kwargs):
    kwargs = dict(task='title_generation', messages=[{'role': 'user', 'content': SECRET}], **kwargs)
    if async_mode:return asyncio.run(aux.async_call_llm(**kwargs))
    return aux.call_llm(**kwargs, fallback_chain=[])


@pytest.mark.parametrize('async_mode', [False, True])
def test_f1_foreign_pool_credential_is_never_laundered(owned, async_mode):
    from agent.credential_pool import load_pool
    assert load_pool('private-slug').peek().runtime_api_key == POOL_KEY
    runtime = {'provider': 'custom:private-slug', 'model': 'fixture-model', 'base_url': OWN_URL, 'api_key': POOL_KEY}
    with named_private_route('private-slug', FOREIGN_URL):
        try:invoke(async_mode, base_url=FOREIGN_URL, main_runtime=runtime)
        except (ValueError, RuntimeError):pass  # a foreign keyless route may fail closed
    assert all(r.headers.get('authorization') != 'Bearer ' + POOL_KEY for r in owned.requests), 'F1 foreign origin received source pool bearer'
    assert all('x-owned-route' not in r.headers for r in owned.requests)


@pytest.mark.parametrize('async_mode', [False, True])
@pytest.mark.parametrize('provider', ['custom:private-slug', 'private-slug'])
def test_f2_named_identity_headers_and_pool_rotation_survive_public_route(owned, async_mode, provider):
    for key in (POOL_KEY, 'synthetic-rotated-pool-key'):
        owned.seed(key)
        with named_private_route(provider):
            response = invoke(async_mode)
        assert response.choices[0].message.content == 'ok'
        request = owned.requests[-1]
        assert request.headers.get('x-owned-route') == 'synthetic-owned-header', 'F2 named provider headers lost'
        assert request.headers['authorization'] == 'Bearer ' + key
        assert request.url.host == 'owned.fixture.invalid'


@pytest.mark.parametrize('provider', ['custom:private-slug', 'private-slug'])
def test_async_named_rotation_on_same_event_loop(owned, provider):
    async def scenario():
        for key in (POOL_KEY, 'synthetic-same-loop-rotated-key'):
            owned.seed(key)
            with named_private_route(provider):
                response = await aux.async_call_llm(task='title_generation',
                    messages=[{'role': 'user', 'content': SECRET}])
            assert response.choices[0].message.content == 'ok'
            request = owned.requests[-1]
            assert request.headers['authorization'] == 'Bearer ' + key
            assert request.headers['x-owned-route'] == 'synthetic-owned-header'
            assert request.url.host == 'owned.fixture.invalid'
    asyncio.run(scenario())
    assert len(owned.requests) == 2


@pytest.mark.parametrize('async_mode', [False, True])
@pytest.mark.parametrize('private', [False, True])
def test_normal_named_and_explicit_alias_credentials_preserved(owned, async_mode, private):
    runtime = {'provider': 'custom:private-slug', 'model': 'fixture-model', 'base_url': OWN_URL, 'api_key': POOL_KEY}
    with incognito_scope(private):
        response = invoke(async_mode, provider='custom:own-alias', main_runtime=runtime)
    request = owned.requests[-1]
    assert response.choices[0].message.content == 'ok'
    assert request.headers['authorization'] == 'Bearer synthetic-alias-key'
    assert request.headers['x-alias'] == 'yes'
    assert request.url.host == 'foreign.fixture.invalid'
    assert not is_incognito()


@pytest.mark.parametrize('async_mode', [False, True])
@pytest.mark.parametrize('provider', ['custom:private-slug', 'private-slug'])
def test_f1_normal_warm_cache_cannot_bypass_private_key_and_header_policy(owned, async_mode, provider):
    async def scenario():
        async def call():
            kwargs: dict = dict(task='mcp', provider=provider, model='fixture-model', base_url=FOREIGN_URL,
                          messages=[{'role': 'user', 'content': SECRET}])
            return await aux.async_call_llm(**kwargs) if async_mode else aux.call_llm(**kwargs, api_mode='chat_completions', fallback_chain=[])
        assert (await call()).choices[0].message.content == 'ok'
        warm = owned.requests[-1]
        assert warm.headers['authorization'] == 'Bearer ' + POOL_KEY
        assert warm.headers['x-owned-route'] == 'synthetic-owned-header'
        private_start = len(owned.requests)
        with named_private_route(provider, FOREIGN_URL):
            try:await call()
            except (ValueError, RuntimeError):pass
        private_requests = owned.requests[private_start:]
        retain_evidence('warm-'+provider.replace(':', '-')+'-'+str(async_mode)+'.json', json.dumps({
            'warm_headers': dict(warm.headers), 'private_headers': [dict(r.headers) for r in private_requests]}, indent=2))
        assert all(r.headers.get('authorization') != 'Bearer ' + POOL_KEY for r in private_requests), 'F1 normal-warmed private cache leaked pool credential'
        assert all('x-owned-route' not in r.headers for r in private_requests), 'F1 normal-warmed private cache leaked source headers'
        assert (await call()).choices[0].message.content == 'ok'
        assert owned.requests[-1].headers['authorization'] == 'Bearer ' + POOL_KEY
        assert owned.requests[-1].headers['x-owned-route'] == 'synthetic-owned-header'
    asyncio.run(scenario())


def retain_evidence(name, content):
    # Optional passive readback survives the native runner's per-file temp cleanup.
    if root := os.environ.get('AUX_RUN_DIR'):
        (Path(root) / name).write_text(content)


@pytest.fixture
def connected_events(monkeypatch):
    from tools import mcp_tool as core
    from tools.mcp_tool_scope import _server_key
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    def run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()
        loop.close()
    thread = threading.Thread(target=run)
    thread.start()
    assert ready.wait(5), 'owned MCP loop failed to start'
    monkeypatch.setattr(core, '_mcp_loop', loop)
    server = asyncio.run_coroutine_threadsafe(prepared_server(), loop).result(5)
    server._config['events'] = True
    monkeypatch.setitem(core._servers, _server_key(server.name), server)
    try:
        yield server
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        assert not thread.is_alive()


@pytest.fixture
def log_sink(tmp_path):
    from agent.incognito import install_logging_filter
    path = tmp_path / 'owned-agent.log'
    handler = logging.FileHandler(path)
    install_logging_filter()
    log = logging.getLogger('tools.mcp_tool')
    previous = log.level
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    try:
        yield path, handler
    finally:
        handler.flush()
        retain_evidence(tmp_path.name + '-sink.log', path.read_text())
        handler.close()
        log.removeHandler(handler)
        log.setLevel(previous)


async def prepared_server():
    server = MCPServerTask('synthetic-server')
    assert await server._prepare_run({'command': 'synthetic-never-launched', 'sampling': {'enabled': True},
                                      'elicitation': {'enabled': True}})
    assert server._sampling is not None and server._elicitation is not None
    return server


def sample_params(model='fixture-model'):
    return NS(model_preferences=NS(hints=[NS(name=model)]),
              messages=[NS(role='user', content=NS(type='text', text='synthetic sampling input'))], max_tokens=10)


async def receive(server, kind, params):
    if kind == 'sampling':coroutine = server._sampling(None, params)
    elif kind == 'elicitation':coroutine = server._elicitation(None, params)
    elif kind == 'message':coroutine = server._make_message_handler()(params)
    else:coroutine = server._make_logging_callback()(params)
    return await contextvars.Context().run(asyncio.create_task, coroutine)


@pytest.mark.parametrize('kind', ['elicitation', 'log', 'message', 'log_parse_error'])
def test_f3_callbacks_use_private_owner_for_admission_payload_and_errors(log_sink, monkeypatch, kind):
    from tools import approval_prompt
    def approval_error(*args, **kwargs):raise RuntimeError(SECRET)
    monkeypatch.setattr(approval_prompt, 'request_elicitation_consent', approval_error)
    async def scenario():
        server = await prepared_server()
        async def delivered_outside_owner():
            if kind == 'elicitation':
                await server._elicitation(None, NS(mode='form', message=SECRET, requestedSchema={}))
            elif kind == 'message':await server._make_message_handler()(RuntimeError(SECRET))
            elif kind == 'log_parse_error':
                class Bad:
                    @property
                    def level(self):raise ValueError(SECRET)
                await server._make_logging_callback()(Bad())
            else:await server._make_logging_callback()(NS(level='warning', data=SECRET, logger=SECRET))
        with incognito_scope(True):
            async with handlers._track_inflight_rpc(server, server.name, 'fixture'):
                await contextvars.Context().run(asyncio.create_task, delivered_outside_owner())
        assert server._pending_call_context is None and not server._inflight_tasks
    asyncio.run(scenario())
    log_sink[1].flush()
    assert SECRET not in log_sink[0].read_text(), 'F3 private callback escaped owner context into actual FileHandler'


@pytest.mark.parametrize('kind', ['log', 'message', 'sampling', 'elicitation'])
def test_f3_unowned_late_callbacks_are_payload_safe(log_sink, monkeypatch, kind):
    from tools import approval_prompt
    consents = []
    monkeypatch.setattr(approval_prompt, 'request_elicitation_consent', lambda *a, **k: consents.append((a, k)) or 'decline')
    async def scenario():
        server = await prepared_server()
        with incognito_scope(True):
            async with handlers._track_inflight_rpc(server, server.name, 'fixture'):
                pass
        # Receiver task has no private ambient context after the call completes.
        if kind == 'log':await server._make_logging_callback()(NS(level='warning', data=SECRET, logger=SECRET))
        elif kind == 'message':await server._make_message_handler()(RuntimeError(SECRET))
        elif kind == 'sampling':
            server._sampling.allowed_models = [SECRET]
            await server._sampling(None, NS(model_preferences=NS(hints=[NS(name=SECRET+'-denied')]), messages=[], max_tokens=10))
        else:await server._elicitation(None, NS(mode='form', message=SECRET, requestedSchema={}))
    asyncio.run(scenario())
    log_sink[1].flush()
    assert not consents, 'F3 late unowned elicitation must not request consent with private payload'
    assert SECRET not in log_sink[0].read_text(), 'F3 late callback payload persisted without owner'


def test_owned_normal_callbacks_still_function(log_sink, monkeypatch):
    from tools import approval_prompt
    calls = []
    def consent(*a, **k):calls.append(is_incognito());return 'accept'
    monkeypatch.setattr(approval_prompt, 'request_elicitation_consent', consent)
    async def scenario():
        server = await prepared_server()
        async with handlers._track_inflight_rpc(server, server.name, 'fixture'):
            await contextvars.Context().run(asyncio.create_task, server._make_logging_callback()(NS(level='warning', data='normal-fixture', logger=None)))
            result = await contextvars.Context().run(asyncio.create_task, server._elicitation(None, NS(mode='form', message='normal-fixture', requestedSchema={})))
            assert result.action == 'accept'
    asyncio.run(scenario())
    log_sink[1].flush()
    assert calls == [False]
    assert 'normal-fixture' in log_sink[0].read_text()


def test_f4_private_event_writes_refused_before_consent_or_persistence(owned, monkeypatch, connected_events):
    from tools import approval_prompt
    consents = []
    monkeypatch.setattr(approval_prompt, 'request_elicitation_consent', lambda *a, **k: consents.append((a, k)) or 'accept')
    server = connected_events
    sent = []
    class WireSession:
        async def send_request(self, request, result_type):
            sent.append(request.method)
            return {'id': 'synthetic-grant', 'refreshBefore': None}
    server.session = WireSession()
    before = {p.relative_to(owned.home): p.read_bytes() for p in owned.home.rglob('*') if p.is_file()}
    with incognito_scope(True):
        result = json.loads(events._make_subscribe_event_handler(server.name, 5)({
            'name': SECRET, 'instructions': SECRET, 'arguments': {'private': SECRET}}))
        removed = json.loads(events._make_unsubscribe_event_handler(server.name, 5)({'name': SECRET}))
    after = {p.relative_to(owned.home): p.read_bytes() for p in owned.home.rglob('*') if p.is_file()}
    retain_evidence('event-write-readback.json', json.dumps({
        'before': {str(k): v.decode() for k, v in before.items()},
        'after': {str(k): v.decode() for k, v in after.items()},
        'sent': sent, 'consents': consents, 'results': [result, removed]}, default=str, indent=2))
    assert not consents, 'F4 private subscribe requested durable consent before rejection'
    assert not sent, 'F4 private event dispatched before rejection'
    assert 'error' in result and 'error' in removed
    assert SECRET not in json.dumps([result, removed])
    assert before == after, 'F4 private event modified owned durable profile'


def test_f4_events_rpc_has_private_owner_and_cleans_up_on_cancel(log_sink, offline_mcp):
    server = offline_mcp.server
    checks = []
    async def observe(peer, request):
        if request.method != 'events/list':return
        owner = peer.server._pending_call_context
        checks.append(owner is not None and owner.get(__import__('agent.incognito', fromlist=['_scope'])._scope, False))
        await peer.emit('notifications/message', {'level': 'warning', 'data': SECRET})
        for task in tuple(peer.server._inflight_tasks):task.cancel()
    offline_mcp.observers.append(observe)
    with incognito_scope(True):
        result = json.loads(events._make_list_event_types_handler(server.name, 5)({}))
    log_sink[1].flush()
    assert 'error' in result
    assert checks == [True], 'F4 events/list has no owning RPC context'
    assert server._pending_call_context is None and not server._inflight_tasks
    assert SECRET not in log_sink[0].read_text()


def test_f3_concurrent_owner_cancel_does_not_restore_finished_private_context(log_sink):
    async def scenario():
        server = await prepared_server()
        entered = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()
        async def hold(index, private):
            with incognito_scope(private):
                async with handlers._track_inflight_rpc(server, server.name, 'fixture'):
                    entered[index].set()
                    await release.wait()
        first = asyncio.create_task(hold(0, True));await entered[0].wait()
        second = asyncio.create_task(hold(1, False));await entered[1].wait()
        await contextvars.Context().run(asyncio.create_task, server._make_logging_callback()(NS(level='warning', data=SECRET, logger=None)))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):await first
        release.set();await second
        assert server._pending_call_context is None, 'F3 completed owner restored by overlapping RPC cleanup'
        assert not server._inflight_tasks
    asyncio.run(scenario())
    log_sink[1].flush()
    assert SECRET not in log_sink[0].read_text()


def test_provider_error_sink_private_on_off_restores_normal_logging(owned, log_sink):
    from agent.credential_pool import load_pool
    with incognito_scope(True):
        owned.fail.append(True)
        with pytest.raises(Exception):invoke(False)
        pool = load_pool('private-slug')
        pool._adopt(pool.peek(), last_error_message=SECRET, last_error_reason=SECRET + ' reason')
        assert SECRET not in (owned.home / 'auth.json').read_text()
    owned.fail.clear()
    assert invoke(False).choices[0].message.content == 'ok'
    logging.getLogger('tools.mcp_tool').warning('normal-after-private')
    log_sink[1].flush()
    assert SECRET not in log_sink[0].read_text()
    assert 'normal-after-private' in log_sink[0].read_text()
    assert not is_incognito()


def test_private_notifications_are_not_parsed_or_refreshed(log_sink):
    touched = []
    class Payload:
        def __getattribute__(self, name):
            touched.append(name)
            raise RuntimeError(SECRET)
    async def scenario():
        server = await prepared_server()
        with incognito_scope(True):
            async with handlers._track_inflight_rpc(server, server.name, 'tools/call'):
                await receive(server, 'logging', Payload())
                await receive(server, 'message', Payload())
        assert not server._pending_refresh_tasks
    asyncio.run(scenario())
    assert not touched
    log_sink[1].flush()
    assert SECRET not in log_sink[0].read_text()


def test_normal_owned_sampling_still_constructs_real_sdk_request(owned, log_sink):
    owned.cfg['auxiliary']['mcp'] = {'provider': 'custom:private-slug', 'model': 'fixture-model'}
    (owned.home / 'config.yaml').write_text(yaml.safe_dump(owned.cfg))
    async def scenario():
        server = await prepared_server()
        async with handlers._track_inflight_rpc(server, server.name, 'tools/call'):
            return await receive(server, 'sampling', sample_params())
    result = asyncio.run(scenario())
    assert result.content.text == 'ok'
    assert owned.requests[-1].headers['authorization'] == 'Bearer ' + POOL_KEY
    assert owned.requests[-1].headers['x-owned-route'] == 'synthetic-owned-header'
    assert not is_incognito()


@pytest.mark.parametrize('private_first', [True, False])
def test_ambiguous_and_late_callbacks_fail_closed_then_normal_owner_recovers(owned, log_sink, monkeypatch, private_first):
    from tools import approval_prompt
    consents = []
    monkeypatch.setattr(approval_prompt, 'request_elicitation_consent',
                        lambda *a, **kw: consents.append(a) or 'accept')
    async def scenario():
        server = await prepared_server()
        entered = [asyncio.Event(), asyncio.Event()]
        hold = asyncio.Event()
        async def owner(private, index):
            with incognito_scope(private):
                async with handlers._track_inflight_rpc(server, server.name, 'tools/call'):
                    entered[index].set()
                    await hold.wait()
        async def forbidden_callbacks():
            assert server._pending_call_context is None
            await receive(server, 'logging', NS(level='warning', data=SECRET, logger=SECRET))
            await receive(server, 'message', RuntimeError(SECRET))
            sampled = await receive(server, 'sampling', sample_params(SECRET))
            elicited = await receive(server, 'elicitation', NS(mode='form', message=SECRET, requestedSchema={}))
            assert sampled.code == -32600 and elicited.action == 'decline'
            assert not consents and not owned.requests
        first = asyncio.create_task(owner(True, 0))
        await entered[0].wait()
        second = asyncio.create_task(owner(False, 1))
        await entered[1].wait()
        await forbidden_callbacks()
        cancelled, remaining = (first, second) if private_first else (second, first)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):await cancelled
        # A completed overlapping owner must not make the remaining cohort attributable.
        await forbidden_callbacks()
        hold.set();await remaining
        assert not server._inflight_tasks
        await forbidden_callbacks()  # late SDK receive-loop callbacks, with ambient normal context
        # Recovery is on a clean normal source, never by relabeling old private ingress.
        server = await prepared_server()
        async with handlers._track_inflight_rpc(server, server.name, 'tools/call'):
            await receive(server, 'logging', NS(level='warning', data='normal-owner-recovered', logger=None))
            result = await receive(server, 'elicitation', NS(mode='form', message='normal-owner-consent', requestedSchema={}))
            assert result.action == 'accept'
        assert len(consents) == 1 and not server._inflight_tasks and server._pending_call_context is None
    asyncio.run(scenario())
    log_sink[1].flush()
    assert SECRET not in log_sink[0].read_text()
    assert 'normal-owner-recovered' in log_sink[0].read_text()


def test_normal_event_persistence_and_private_read_remain_usable(owned, offline_mcp, monkeypatch):
    from tools import approval_prompt
    server = offline_mcp.server
    consents, sent, contexts = [], [], []
    monkeypatch.setattr(approval_prompt, 'request_elicitation_consent',
                        lambda *a, **kw: consents.append(a) or 'accept')
    async def observe(peer, request):
        if not request.method.startswith('events/'):return
        context = peer.server._pending_call_context
        assert context is not None
        contexts.append(context.run(is_incognito))
        sent.append(request.method)
    offline_mcp.observers.append(observe)
    args = {'name': 'synthetic-public-event', 'instructions': 'synthetic public instruction', 'arguments': {'public': 'yes'}}
    subscribe = events._make_subscribe_event_handler(server.name, 5)
    unsubscribe = events._make_unsubscribe_event_handler(server.name, 5)
    listing = events._make_list_event_types_handler(server.name, 5)
    assert 'subscribed' in json.loads(subscribe(args))
    persisted = {str(p.relative_to(owned.home)): p.read_text() for p in owned.home.rglob('*') if p.is_file()}
    assert 'synthetic public instruction' in json.dumps(persisted)
    before = {p.relative_to(owned.home): p.read_bytes() for p in owned.home.rglob('*') if p.is_file()}
    with incognito_scope(True):
        result = json.loads(listing({}))
        refused = json.loads(unsubscribe(args))
    after = {p.relative_to(owned.home): p.read_bytes() for p in owned.home.rglob('*') if p.is_file()}
    assert before == after and 'error' in refused
    assert result['events'][0]['name'] == args['name']
    assert result['subscriptions'][0]['arguments'] == args['arguments']
    assert 'unsubscribed' in json.loads(unsubscribe(args))
    assert events._load_routes(server.name) == {}
    assert len(consents) == 1 and contexts == [False, True, False]
    assert sent == ['events/subscribe', 'events/list', 'events/unsubscribe']
    assert server._pending_call_context is None and not server._inflight_tasks
    retain_evidence('normal-event-persistence-readback.json', json.dumps({
        'persisted': persisted, 'private_read': result, 'private_write_refusal': refused,
        'after_unsubscribe': events._load_routes(server.name), 'sent': sent, 'contexts': contexts}, indent=2))
