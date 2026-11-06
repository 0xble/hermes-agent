"""Synthetic transport fixture; source factories, SDK validation and persistence remain native."""
import asyncio
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
import anyio
import pytest
from tools import mcp_tool as core
from tools.mcp_tool import MCPServerTask

PRIVATE = 'SYNTHETIC_PRIVATE_INGRESS_8337'
NORMAL = 'synthetic-normal-idle-ingress'

@pytest.fixture
def offline_mcp(monkeypatch):
    """Only the stdio transport is doubled: session, handshake and dispatch are native."""
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCRequest, JSONRPCResponse, JSONRPCNotification
    from tools.mcp_tool_scope import _server_key
    peers, observers = [], []
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def drive():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=drive)
    thread.start()
    assert ready.wait(5)
    monkeypatch.setattr(core, '_mcp_loop', loop)
    core._ensure_mcp_sdk()

    async def run_transport(server, config):
        @asynccontextmanager
        async def wire():
            incoming, reader = anyio.create_memory_object_stream(100)
            writer, outgoing = anyio.create_memory_object_stream(100)
            peer = NS(server=server, incoming=incoming, requests=[], callbacks=[], closed=False, release=asyncio.Event())
            peers.append(peer)

            async def emit(method, params, request_id=None):
                message = (JSONRPCNotification(jsonrpc='2.0', method=method, params=params) if request_id is None
                           else JSONRPCRequest(jsonrpc='2.0', id=request_id, method=method, params=params))
                await incoming.send(SessionMessage(message=message))
            peer.emit = emit

            async def pump():
                async with outgoing:
                    async for item in outgoing:
                        message = item.message
                        if not isinstance(message, JSONRPCRequest):
                            if isinstance(message, JSONRPCResponse):peer.callbacks.append(message)
                            continue
                        peer.requests.append(message)
                        for observe in observers:await observe(peer, message)
                        if message.method == 'initialize':
                            result = {'protocolVersion': message.params['protocolVersion'],
                                      'capabilities': {'resources': {}, 'tools': {'listChanged': True}},
                                      'serverInfo': {'name': 'synthetic-offline-server', 'version': '1'}}
                        elif message.method == 'tools/list':result = {'tools': []}
                        elif message.method == 'events/list':result = {'events': [{'name': 'synthetic-public-event'}]}
                        elif message.method == 'events/subscribe':result = {'id': 'synthetic-public-grant'}
                        elif message.method == 'resources/read':
                            marker = NORMAL if message.params['uri'].startswith('fixture://normal/') else PRIVATE
                            case = message.params['uri'].rsplit('/', 1)[-1]
                            if case == 'hold':await peer.release.wait()
                            if case in {'malformed-notify', 'malformed-request', 'malformed-wire', 'stream-error'}:
                                if case == 'malformed-notify':
                                    await emit('notifications/message', {'level': 'warning', 'data': 'fixture', 'logger': {'private': marker}})
                                elif case == 'malformed-request':
                                    await emit('sampling/createMessage', {'messages': [{'role': 'user', 'content': {'type': 'text', 'text': marker}}], 'maxTokens': {'private': marker}}, 'malformed-private-input')
                                elif case == 'malformed-wire':
                                    from mcp.client.stdio import _parse_line
                                    await incoming.send(_parse_line(json.dumps({'jsonrpc': '2.0', 'method': {'invalid': marker}})))
                                else:await incoming.send(RuntimeError(marker))
                                # SDK notification processing must complete before this response is observed.
                                # Yield behind spawned SDK callback tasks, while the actual RPC is still active.
                                for _ in range(8):
                                    step = asyncio.Event()
                                    asyncio.get_running_loop().call_soon(step.set)
                                    await step.wait()
                            result = {'contents': [{'uri': message.params['uri'], 'text': 'synthetic read result'}]}
                        else:result = {}
                        await incoming.send(SessionMessage(message=JSONRPCResponse(jsonrpc='2.0', id=message.id, result=result)))
            task = asyncio.create_task(pump())
            try:
                yield reader, writer
            finally:
                peer.closed = True
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
                await incoming.aclose()
                await reader.aclose()
                await writer.aclose()
        return await server._serve_transport(wire(), 'synthetic wire', 5)

    monkeypatch.setattr(MCPServerTask, '_run_stdio', run_transport)
    server = MCPServerTask('synthetic-ingress')
    future = asyncio.run_coroutine_threadsafe(server.start({'command': 'synthetic-never-executed'}), loop)
    future.result(10)
    monkeypatch.setitem(core._servers, _server_key(server.name), server)

    def run(coro):return asyncio.run_coroutine_threadsafe(coro, loop).result(10)

    async def barrier():
        # Drain scheduled SDK tasks deterministically: schedule behind them, then a second turn
        # for the callback's own context-replayed task.
        for _ in range(8):
            event = asyncio.Event()
            loop.call_soon(event.set)
            await event.wait()
    try:
        yield NS(server=server, peers=peers, run=run, barrier=barrier, observers=observers)
    finally:
        run(server.shutdown())
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        assert not thread.is_alive()
        assert all(peer.closed for peer in peers)
