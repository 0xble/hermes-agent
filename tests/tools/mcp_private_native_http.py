"""Offline HTTP byte transport only; Hermes factories/OAuth/storage and SDK stay real."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.incognito import is_incognito, install_logging_filter
from tools import mcp_tool as core

PRIVATE = 'SYNTHETIC_PRIVATE_TRANSPORT_6159'
NORMAL = 'synthetic-normal-transport-resumed'
URL = 'https://mcp.synthetic.invalid/mcp'
ISSUER = 'https://mcp.synthetic.invalid'


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes().hex()
            for p in root.rglob('*') if p.is_file()}


@pytest.fixture
def native_http(monkeypatch, tmp_path):
    import logging
    import httpx2 as http
    from tools import mcp_oauth_manager as managers
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow, dashboard_oauth_flow
    core._ensure_mcp_sdk()
    install_logging_filter()
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    manager = managers.MCPOAuthManager()
    monkeypatch.setattr(managers, '_MANAGER', manager)
    records, streams, clients, providers = [], [], [], []
    grants = {}
    effects = []
    sink = tmp_path / 'transport.log'
    handler = logging.FileHandler(sink)
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    current = SimpleNamespace(case='valid', hold=asyncio.Event(), entered=asyncio.Event())

    class Consent(DashboardOAuthFlow):
        async def publish_authorization_url(self, url):
            await super().publish_authorization_url(url)
            self.deliver_callback(code='synthetic-code', state=self.expected_state, error=None, iss=ISSUER)

    class QueueStream(http.AsyncByteStream):
        def __init__(self, initial=b''):
            self.queue = asyncio.Queue()
            self.closed = False
            streams.append(self)
            if initial:self.queue.put_nowait(initial)
        async def __aiter__(self):
            try:
                while True:yield await self.queue.get()
            finally:self.closed = True
        async def aclose(self):self.closed = True

    def event(value):return ('event: message\ndata: ' + json.dumps(value) + '\n\n').encode()

    async def respond(request):
        private = is_incognito()
        marker = PRIVATE if private else NORMAL
        path = request.url.path
        body = await request.aread()
        records.append({'private': private, 'method': request.method, 'url': str(request.url),
                        'authorization': request.headers.get('authorization'), 'body': body.decode()})
        if '.well-known/oauth-protected-resource' in path:
            return http.Response(200, json={'resource': URL, 'authorization_servers': [ISSUER]})
        if '.well-known/' in path:
            return http.Response(200, json={'issuer': ISSUER, 'authorization_endpoint': ISSUER + '/authorize',
                'token_endpoint': ISSUER + '/token', 'registration_endpoint': ISSUER + '/register',
                'response_types_supported': ['code'], 'code_challenge_methods_supported': ['S256'],
                'grant_types_supported': ['authorization_code', 'refresh_token']})
        if path == '/register':
            return http.Response(201, json={'client_id': marker, 'redirect_uris': ['https://callback.synthetic.invalid/callback'],
                'token_endpoint_auth_method': 'none', 'grant_types': ['authorization_code', 'refresh_token'],
                'response_types': ['code']})
        if path == '/token':
            if current.case == 'reject-refresh' and b'grant_type=refresh_token' in body:
                return http.Response(400, json={'error': 'invalid_client', 'error_description': marker})
            from urllib.parse import parse_qs
            params = parse_qs(body.decode())
            if params.get('grant_type') == ['refresh_token']:
                old = params['refresh_token'][0]
                if old not in grants:return http.Response(400, json={'error': 'invalid_grant'})
                owner, generation = grants.pop(old)
            else:owner, generation = marker, -1
            grant = owner + '-refresh-' + str(generation + 1)
            grants[grant] = (owner, generation + 1)
            return http.Response(200, json={'access_token': owner, 'refresh_token': grant,
                'token_type': 'Bearer', 'expires_in': 3600})
        if not request.headers.get('authorization'):
            return http.Response(401, headers={'www-authenticate': 'Bearer resource_metadata="' + ISSUER + '/.well-known/oauth-protected-resource"'})
        if request.method == 'DELETE':return http.Response(200)
        if request.method == 'GET':
            session = str(len(streams))
            stream = QueueStream(('event: endpoint\ndata: /messages?sessionId=' + session + '\n\n').encode())
            if not hasattr(current, 'sse'):current.sse = {}
            current.sse[session] = stream
            return http.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        return await mcp_response(request, body, private, marker, path)

    async def initialize_result(request, message, private, marker, path):
        result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'resources': {}},
                  'serverInfo': {'name': 'synthetic-offline-http', 'version': '1'}}
        return result

    async def read_result(request, message, private, marker, path):
        current.entered.set()
        if current.case == 'hold':await current.hold.wait()
        if current.case == 'failure':raise http.ReadError(marker)
        if current.case == 'expired' and private:
            response = {'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32001, 'message': 'session expired ' + marker}}
            if path == '/messages':
                current.sse[str(request.url.params['sessionId'])].queue.put_nowait(event(response))
                return http.Response(202)
            return http.Response(200, json=response)
        result = {'contents': [{'uri': message['params']['uri'], 'text': marker}]}
        return result

    async def call_result(request, message, private, marker, path):
        effects.append({'private': private, 'args': message['params']['arguments']})
        if private and current.case == 'write-loss':raise http.ReadError(marker)
        if private and current.case == 'write-expired':
            return http.Response(200, json={'jsonrpc': '2.0', 'id': message['id'],
                'error': {'code': -32001, 'message': 'session expired ' + marker}})
        result = {'content': [{'type': 'text', 'text': marker}], 'isError': current.case == 'tool-error'}
        return result

    async def mcp_response(request, body, private, marker, path):
        message = json.loads(body)
        if 'id' not in message:return http.Response(202)
        method = message['method']
        handlers = {'initialize': initialize_result, 'resources/read': read_result, 'tools/call': call_result}
        handler = handlers.get(method)
        if handler is not None:
            result = await handler(request, message, private, marker, path)
            if isinstance(result, http.Response):
                return result
        else:
            result = {
                'tools/list': {'tools': [{'name': 'write', 'inputSchema': {'type': 'object'}}]},
                'resources/list': {'resources': []}, 'prompts/list': {'prompts': []},
            }.get(method, {})
        response = {'jsonrpc': '2.0', 'id': message['id'], 'result': result}
        if request.url.path == '/messages':
            if current.case == 'malformed' and method == 'resources/read':
                current.sse[str(request.url.params['sessionId'])].queue.put_nowait(event({'jsonrpc': '2.0', 'method': {'invalid': marker}}))
            current.sse[str(request.url.params['sessionId'])].queue.put_nowait(event(response))
            return http.Response(202)
        if current.case == 'malformed' and method == 'resources/read':
            return http.Response(200, headers={'content-type': 'text/event-stream'},
                content=event({'jsonrpc': '2.0', 'method': {'invalid': marker}}) + event(response))
        return http.Response(200, json=response)

    original_init = http.AsyncClient.__init__
    def init(client, *args, **kwargs):
        kwargs['transport'] = http.MockTransport(respond)
        original_init(client, *args, **kwargs)
        clients.append(client)
        if client.auth is not None:providers.append(client.auth)
    monkeypatch.setattr(http.AsyncClient, '__init__', init)
    def consent():
        return dashboard_oauth_flow(Consent('synthetic-flow', 'synthetic', None, str(home),
                                           'https://callback.synthetic.invalid/callback'))
    try:
        yield SimpleNamespace(home=home, manager=manager, records=records, streams=streams, clients=clients,
            providers=providers, effects=effects, current=current, consent=consent, snapshot=lambda: snapshot(home),
            flush=handler.flush, log=lambda: (handler.flush(), sink.read_text())[1], http=http, tmp=tmp_path,
            respond=respond, original_client_init=original_init)
    finally:
        handler.flush()
        root.removeHandler(handler)
        handler.close()
        root.setLevel(previous)
        import os
        if output := os.environ.get('AUX_RUN_DIR'):
            dest = Path(output) / tmp_path.name
            dest.mkdir()
            (dest / 'disk.json').write_text(json.dumps(snapshot(home), indent=2))
            (dest / 'sink.log').write_bytes(sink.read_bytes())
            (dest / 'wire.json').write_text(json.dumps(records, indent=2))
        assert all(client.is_closed for client in clients)
        assert all(stream.closed for stream in streams)
