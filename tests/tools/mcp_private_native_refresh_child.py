"""Disposable normal OAuth worker for cross-process refresh-fence qualification."""
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs

# Match tests/conftest.py's declared synthetic version fixture; this helper is
# a native child, not pytest, and must not perform unrelated Git discovery.
from hermes_cli import version_info
version_info._cached_version_info = version_info.VersionInfo(
    'unknown', 'git.0000000', None, '0' * 40, 'main', 'git')

from tools.mcp_tool import sdk_httpx
from tools.mcp_oauth import HermesTokenStorage
from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS
from mcp.shared.auth import OAuthClientMetadata


async def main():
    http = sdk_httpx()
    authority = Path(sys.argv[1])
    seen = []
    async def peer(request):
        if request.url.path == '/token':
            state = json.loads(authority.read_text())
            params = parse_qs((await request.aread()).decode())
            assert params['refresh_token'] == ['synthetic-r0'], params
            assert state['posts'] == 0, state
            authority.write_text(json.dumps({'posts': 1, 'refresh': 'synthetic-r1'}))
            return http.Response(200, json={'access_token': 'synthetic-new-access', 'refresh_token': 'synthetic-r1',
                                            'token_type': 'Bearer', 'expires_in': 3600})
        seen.append(request.headers.get('authorization'))
        return http.Response(200, json={'ok': True})
    async def no_redirect(url):raise AssertionError('normal refresh unexpectedly required consent')
    async def no_callback():raise AssertionError('normal refresh unexpectedly required callback')
    provider = _HERMES_PROVIDER_CLS(server_name='synthetic-cross-process', server_url='https://refresh.synthetic.invalid/mcp',
        client_metadata=OAuthClientMetadata(redirect_uris=['https://callback.synthetic.invalid/callback']),
        storage=HermesTokenStorage('synthetic-cross-process'), redirect_handler=no_redirect, callback_handler=no_callback)
    await provider._initialize()
    print('ready', flush=True)
    assert sys.stdin.readline().strip() == 'go'
    async with http.AsyncClient(transport=http.MockTransport(peer), auth=provider) as client:
        response = await client.get('https://refresh.synthetic.invalid/mcp')
        assert response.status_code == 200
    assert seen == ['Bearer synthetic-new-access'], seen
    assert provider._hermes_fence is None
    print(json.dumps({'authorization': seen, 'fence': None}), flush=True)


if __name__ == '__main__':asyncio.run(main())
