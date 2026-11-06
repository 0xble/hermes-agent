"""Connection-owned OAuth state: read saved credentials, never write private results back.

Private discovery, authorization and request state remain volatile. A saved grant
may be renewed only by the separate auth-only maintenance boundary below, under
its normal generation fence and using independently saved authorization inputs.
Independently authorized private grants refresh entirely in memory.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from tools.mcp_oauth import HermesTokenStorage


class PrivateTokenStorage(HermesTokenStorage):
    volatile = True

    def __init__(self, server_name: str, *, hermes_home=None):
        super().__init__(server_name, hermes_home=hermes_home)
        self.owns_tokens = False
        self._memory = {}
        # Snapshot a fixed allowlist, never enumerate the user's token directory.
        for path in (*self._state_paths(), self._cimd_rejected_path()):
            try:self._memory[path.name] = path.read_bytes()
            except OSError:pass

    def _load_model(self, *args, **kwargs):
        # Validation diagnostics can include rejected input. Ownership follows
        # storage even when a delayed callback runs outside its caller Context.
        from agent.incognito import incognito_scope
        with incognito_scope(True):return super()._load_model(*args, **kwargs)

    def _read_state(self, path):
        raw = self._memory.get(path.name)
        if raw is None:return None
        try:return json.loads(raw.decode('utf-8-sig'))
        except (UnicodeError, ValueError):return None

    def _write_state(self, path, data):
        self._memory[path.name] = json.dumps(data, default=str).encode()

    async def set_tokens(self, tokens):
        await super().set_tokens(tokens)
        self.owns_tokens = True

    def discard_tokens_and_metadata(self):
        for path in (self._tokens_path(), self._meta_path()):self._memory.pop(path.name, None)
        self.owns_tokens = False

    def mark_cimd_rejected(self):
        self._memory[self._cimd_rejected_path().name] = b''

    def cimd_rejected(self):
        return self._cimd_rejected_path().name in self._memory

    def remove(self, *, keep_metadata=False):
        for path in (self._tokens_path(), self._client_info_path(), self._cimd_rejected_path(),
                     *(() if keep_metadata else (self._meta_path(),))):
            self._memory.pop(path.name, None)
        self.owns_tokens = False

    def snapshot(self):
        return {p.name: self._memory[p.name] for p in self._state_paths() if p.name in self._memory}

    def restore(self, snapshot, *, only_if_absent=False):
        if only_if_absent and any(p.name in self._memory for p in self._state_paths()):return
        self.remove()
        self._memory.update(copy.deepcopy(snapshot))
        # A rollback cannot establish that this connection minted a grant.

    def poison_client_registration(self):
        name = self._client_info_path().name
        if name not in self._memory:return False
        self._memory[name + '.bak'] = self._memory.pop(name)
        self._memory.pop(self._meta_path().name, None)
        return True

    def has_cached_tokens(self):
        return self._tokens_path().name in self._memory

    def credential_stamp(self):
        return tuple(self._memory.get(p.name) for p in (self._tokens_path(), self._client_info_path()))


def token_storage(server_name, *, hermes_home=None):
    from agent.incognito import is_incognito
    from tools import mcp_oauth
    cls = PrivateTokenStorage if is_incognito() else mcp_oauth.HermesTokenStorage
    return cls(server_name, hermes_home=hermes_home)


class _SavedAuthorizationStorage(HermesTokenStorage):
    """Maintenance owns token renewal only, never discovery/registration/diagnostics."""
    auth_only = True

    def _write_state(self, path, data):
        # SDK initialization may coerce a saved registration; do not persist that
        # or any metadata as a side effect of this narrow maintenance operation.
        if path != self._tokens_path():return
        # OAuth extension fields are not part of saved credential maintenance.
        allowed = {'access_token', 'refresh_token', 'token_type', 'expires_in', 'scope', 'expires_at', 'hermes_issuer'}
        super()._write_state(path, {k: v for k, v in data.items() if k in allowed})


async def renew_saved_authorization(private, server_url: str, token_user_agent=None, auth_policy=None,
                                    normal_resource_policy=None) -> None:
    """Renew independently saved auth, not a private request or private auth Context.

    No request URL/body/headers/challenge/scope/metadata from the private provider
    enters this operation. The sole durable write is the issuer-bound replacement
    token generation from the saved endpoint; logging remains incognito. Missing
    saved issuer/client/metadata fails closed. Legacy audience reconstruction uses
    only independently configured discovery or validated normal-owner authority,
    never private challenge/PRM authority.
    """
    from tools.mcp_oauth import OAuthNonInteractiveError, acquire_refresh_fence
    from tools.mcp_oauth_manager import HermesMCPOAuthProvider
    from tools.mcp_tool import sdk_httpx
    from mcp.shared.auth import OAuthClientMetadata

    storage = _SavedAuthorizationStorage(private._server_name, hermes_home=private._hermes_home)
    fd = await acquire_refresh_fence(storage._tokens_path())
    provider = None
    try:
        tokens = await storage.get_tokens()
        client = await storage.get_client_info()
        metadata = storage.load_oauth_metadata()
        if (tokens is None or not tokens.refresh_token or client is None or metadata is None
                or not storage.loaded_issuer
                or storage.loaded_issuer.rstrip('/') != str(metadata.issuer).rstrip('/')
                or not metadata.token_endpoint):
            raise OAuthNonInteractiveError('Saved MCP authorization cannot be safely renewed; reauthorize outside incognito.')
        from mcp.client.auth.utils import credentials_match_issuer
        if not credentials_match_issuer(client, str(metadata.issuer), None):
            raise OAuthNonInteractiveError('Saved MCP client belongs to a different issuer; reauthorize outside incognito.')
        # A fresh provider, never the public cache entry or the private Context.
        # Registration and requested scopes come exclusively from saved auth.
        client_metadata = OAuthClientMetadata.model_validate(client.model_dump(mode='json', exclude_none=True))
        provider = HermesMCPOAuthProvider(server_url=server_url, client_metadata=client_metadata, storage=storage,
                                         token_user_agent=token_user_agent)
        provider._hermes_fence = fd
        await provider._initialize()
        from tools.mcp_tool import LATEST_HANDSHAKE_VERSION
        policy = auth_policy or {}
        provider.context.protocol_version = policy.get('protocol_version', LATEST_HANDSHAKE_VERSION)
        provider._hermes_auth_policy = policy
        provider._hermes_normal_resource_policy = normal_resource_policy
        if not provider._hermes_live_ttl() or not provider.context.is_token_valid():
            provider._coerce_client_secret_post()
            # The fence already owns read -> SDK request -> shared response -> persist.
            # Do not re-enter/release it via the normal generator entry point.
            request = await provider._hermes_build_refresh_request()
            http = sdk_httpx()
            from tools.mcp_tool_transport import _mcp_http_transport_kwargs
            transport = _mcp_http_transport_kwargs(http, str(request.url),
                policy.get('ssl_verify', True), policy.get('client_cert'))
            async with http.AsyncClient(timeout=10, follow_redirects=False, **transport) as client_http:
                response = await client_http.send(request, follow_redirects=False)
                # Keep the maintenance fence through minimal-authority receipt
                # publication and the private credential snapshot. The public
                # handler releases early for the SDK generator's lifecycle.
                if not await provider._hermes_handle_refresh_response(response):
                    raise OAuthNonInteractiveError('Saved MCP authorization renewal failed; reauthorize outside incognito.')
            if normal_resource_policy is not None:
                normal_resource_policy(storage, server_url, (tokens.refresh_token,
                    provider.context.current_tokens.refresh_token, provider._hermes_resource_policy[1]))
        # Only read the successful saved generation into the private snapshot.
        # It remains a saved grant, not a connection-owned volatile refresh grant.
        for path in private._state_paths():
            try:private._memory[path.name] = path.read_bytes()
            except OSError:private._memory.pop(path.name, None)
    finally:
        if provider is not None:await provider._hermes_release_refresh_fence()
        else:
            from tools.mcp_oauth import release_refresh_fence
            release_refresh_fence(fd)
