"""Synthetic offline wire, real SDK ingress/validation, factories and disk sinks."""
import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from agent.incognito import incognito_scope, install_logging_filter
from tools import mcp_tool_handlers as handlers
from tests.agent.test_auxiliary_private_boundaries import owned  # real offline SDK fixture

PRIVATE = 'SYNTHETIC_PRIVATE_INGRESS_8337'
NORMAL = 'synthetic-normal-idle-ingress'


@pytest.fixture
def disk_sink(tmp_path):
    install_logging_filter()
    path = tmp_path / 'owned-ingress.log'
    handler = logging.FileHandler(path)
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield path, handler
    finally:
        handler.flush()
        if output := os.environ.get('AUX_RUN_DIR'):
            (Path(output) / (tmp_path.name + '-ingress-sink.log')).write_bytes(path.read_bytes())
        root.removeHandler(handler)
        handler.close()
        root.setLevel(previous)


from tests.tools.mcp_private_wire import offline_mcp

def read_handler(server):
    return handlers._make_read_resource_handler(server.name, 10)


@pytest.mark.parametrize('case', ['malformed-notify', 'malformed-request', 'malformed-wire', 'stream-error'])
def test_sdk_private_ingress_before_application_admission(offline_mcp, disk_sink, case):
    fixture = offline_mcp
    with incognito_scope(True):
        result = json.loads(read_handler(fixture.server)({'uri': 'fixture://private/' + case}))
    fixture.run(fixture.barrier())
    assert result['result'] == 'synthetic read result'
    disk_sink[1].flush()
    assert PRIVATE not in disk_sink[0].read_text(), 'SDK pre-admission private payload escaped into actual FileHandler'
    public = fixture.peers[0]
    fixture.run(public.emit('notifications/message', {'level': 'warning', 'data': NORMAL}))
    fixture.run(fixture.barrier())
    disk_sink[1].flush()
    assert NORMAL in disk_sink[0].read_text(), 'normal idle diagnostics did not resume'
    if output := os.environ.get('AUX_RUN_DIR'):
        (Path(output) / (case + '-connection-proof.json')).write_text(json.dumps([
            {'closed': peer.closed, 'methods': [m.method for m in peer.requests],
             'uris': [m.params.get('uri') for m in peer.requests if m.params and 'uri' in m.params]}
            for peer in fixture.peers], indent=2))


@pytest.mark.parametrize('cancelled', [False, True])
@pytest.mark.parametrize('kind', ['log', 'sampling', 'elicitation'])
def test_private_complete_or_cancel_then_unrelated_normal_rpc_has_no_old_owner(offline_mcp, disk_sink, monkeypatch, owned, kind, cancelled):
    fixture = offline_mcp
    consents = []
    from tools import approval_prompt
    monkeypatch.setattr(approval_prompt, 'request_elicitation_consent', lambda *a, **kw: consents.append(a) or 'decline')

    async def sequence():
        server = fixture.server
        with incognito_scope(True):
            try:
                async with handlers._track_inflight_rpc(server, server.name, 'resources/read'):
                    if cancelled:raise asyncio.CancelledError
            except asyncio.CancelledError:pass
        # Genuine application callback entry with delayed private content during unrelated B.
        async with handlers._track_inflight_rpc(server, server.name, 'resources/read B'):
            if kind == 'log':await server._make_logging_callback()(NS(level='warning', data=PRIVATE, logger=None))
            elif kind == 'sampling':
                result = await server._sampling(None, NS(messages=[], max_tokens=10,
                    model_preferences=NS(hints=[NS(name=PRIVATE)])))
                from mcp.types import ErrorData
                assert isinstance(result, ErrorData) and result.code == -32600, 'delayed private sampling was admitted to normal auxiliary route'
            else:await server._elicitation(None, NS(mode='form', message=PRIVATE, requestedSchema={}))
        assert not server._inflight_tasks
    fixture.run(sequence())
    disk_sink[1].flush()
    assert not owned.requests, 'delayed private sampling dispatched through actual SDK'
    assert not consents, 'delayed private elicitation was assigned to unrelated normal owner'
    assert PRIVATE not in disk_sink[0].read_text(), 'delayed private callback persisted under unrelated normal B'



@pytest.mark.parametrize('cancelled', [False, True])
@pytest.mark.parametrize('kind', ['log', 'sampling', 'elicitation'])
def test_public_factories_isolate_private_connection_through_delayed_callbacks(offline_mcp, disk_sink, owned, monkeypatch, kind, cancelled):
    from tests.agent.test_auxiliary_private_boundaries import named_private_route, POOL_KEY
    from mcp.types import ErrorData
    from tools import approval_prompt
    fixture = offline_mcp
    consents, owner_sources = [], []
    entered = threading.Event()
    monkeypatch.setattr(approval_prompt, 'request_elicitation_consent', lambda *a, **kw: consents.append(a) or 'decline')

    async def observe(peer, message):
        if message.method != 'resources/read':return
        if message.params['uri'].endswith('/A'):
            owner_sources.append(peer.server)
            if cancelled:
                for task in tuple(peer.server._inflight_tasks):task.cancel()
        if message.params['uri'].endswith('/hold'):entered.set()
    fixture.observers.append(observe)
    with named_private_route():
        response = json.loads(read_handler(fixture.server)({'uri': 'fixture://private/A'}))
    assert ('error' in response) == cancelled
    old = owner_sources[0]
    answers = []
    worker = threading.Thread(target=lambda: answers.append(read_handler(fixture.server)({'uri': 'fixture://normal/hold'})))
    worker.start()
    assert entered.wait(5)
    async def delayed():
        if kind == 'log':await old._make_logging_callback()(NS(level='warning', data=PRIVATE, logger=None))
        elif kind == 'sampling':
            result = await old._sampling(None, NS(messages=[], max_tokens=10, model_preferences=NS(hints=[NS(name='fixture-model')])))
            assert isinstance(result, ErrorData) and result.code == -32600, 'old A sampling was assigned to B'
        else:await old._elicitation(None, NS(mode='form', message=PRIVATE, requestedSchema={}))
    try:fixture.run(delayed())
    finally:
        fixture.run(_release(fixture.peers[0]))
        worker.join(10)
    assert not worker.is_alive() and len(answers) == 1
    assert not consents and not owned.requests
    assert old is not fixture.server, 'private RPC reused normal connection/SDK receive tasks'
    assert fixture.peers[-1].closed, 'private SDK/transport lifetime survived A completion'
    normal = fixture.peers[0]
    fixture.run(normal.emit('notifications/message', {'level': 'warning', 'data': NORMAL}))
    fixture.run(fixture.barrier())
    async def normal_sample():
        return await fixture.server._sampling(None, NS(messages=[], max_tokens=10, model_preferences=NS(hints=[NS(name='fixture-model')])))
    assert fixture.run(normal_sample()).content.text == 'ok'
    assert len(owned.requests) == 1 and owned.requests[0].headers['authorization'] == 'Bearer ' + POOL_KEY
    disk_sink[1].flush()
    contents = disk_sink[0].read_text()
    assert PRIVATE not in contents and NORMAL in contents
    if output := os.environ.get('AUX_RUN_DIR'):
        (Path(output) / (f'public-{kind}-{cancelled}-lifetimes.json')).write_text(json.dumps({
            'private_closed': fixture.peers[-1].closed, 'normal_open': not normal.closed,
            'source_isolated': old is not fixture.server, 'consents': consents,
            'normal_sdk_requests': len(owned.requests), 'normal_auth_header': owned.requests[0].headers['authorization']}, indent=2))



async def _release(peer):peer.release.set()

@pytest.mark.parametrize('case', ['malformed-notify', 'malformed-wire'])
def test_normal_pre_admission_sdk_diagnostics_still_reach_disk(offline_mcp, disk_sink, case):
    result = json.loads(read_handler(offline_mcp.server)({'uri': 'fixture://normal/' + case}))
    assert result['result'] == 'synthetic read result'
    offline_mcp.run(offline_mcp.barrier())
    disk_sink[1].flush()
    contents = disk_sink[0].read_text()
    assert NORMAL in contents and PRIVATE not in contents


@pytest.mark.parametrize('notification', ['diagnostics', 'tools'])
def test_normal_idle_sdk_diagnostics_and_dynamic_notifications(offline_mcp, disk_sink, notification):
    fixture = offline_mcp
    peer = fixture.peers[0]
    if notification == 'diagnostics':
        fixture.run(peer.emit('notifications/message', {'level': 'warning', 'data': NORMAL}))
        fixture.run(fixture.barrier())
        disk_sink[1].flush()
        assert NORMAL in disk_sink[0].read_text(), 'normal idle SDK logging was dropped'
    else:
        before = sum(request.method == 'tools/list' for request in peer.requests)
        fixture.run(peer.emit('notifications/tools/list_changed', {}))
        fixture.run(fixture.barrier())
        async def drain():
            await asyncio.gather(*tuple(fixture.server._pending_refresh_tasks))
        fixture.run(drain())
        assert sum(request.method == 'tools/list' for request in peer.requests) == before + 1, 'normal idle tools/list_changed never refreshed native SDK discovery'
