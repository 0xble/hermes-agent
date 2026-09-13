"""Anthropic authority and failed-install boundaries, offline."""
import hashlib
from types import SimpleNamespace
import pytest
from tools.custom_subagents import ResolvedRoute, RuntimePin

@pytest.mark.parametrize("key", ["sk-ant-oat01-fixture-only", "fixture-api-key"])
@pytest.mark.parametrize("fallback", [True, False])
def test_effective_authority(key, fallback):
    from agent.anthropic_adapter import build_anthropic_client
    from agent.client_lifecycle import _swap_fallback_clients
    from tools.custom_subagent_fallbacks import frozen_fallback_client
    url = "https://api.anthropic.com"
    digest = hashlib.sha256(key.encode()).hexdigest()
    route = ResolvedRoute("anthropic", "fixture-model", url, "anthropic_messages", None, key, digest, "{}")
    pin = RuntimePin("fixture", "primary" if fallback else route.provider, "primary-model" if fallback else route.model, "https://primary.invalid" if fallback else url, "chat_completions" if fallback else route.api_mode, None, digest, True, fallback_routes=(route,) if fallback else ())
    child = SimpleNamespace(provider=route.provider, model=route.model, base_url=url, api_mode=route.api_mode, api_key=key, request_overrides={})
    if fallback:
        _swap_fallback_clients(child, frozen_fallback_client(pin, route.native_entry()), route.provider, route.model, url, route.api_mode)
        client = child._anthropic_client
    else:
        client = build_anthropic_client(key, url)
    try:
        pin.validate_request(child, {"model": route.model}, client=client)
        field = "auth_token" if client.auth_token else "api_key"
        for value in ("changed-fixture-key", None):
            setattr(client, field, value)
            with pytest.raises(ValueError):
                pin.validate_request(child, {"model": route.model}, client=client)
        setattr(client, field, key)
        other_field = "api_key" if field == "auth_token" else "auth_token"
        setattr(client, other_field, "different-fixture-credential")
        with pytest.raises(ValueError):
            pin.validate_request(child, {"model": route.model}, client=client)
        setattr(client, other_field, None)
        client.base_url = "https://other.invalid"
        with pytest.raises(ValueError):
            pin.validate_request(child, {"model": route.model}, client=client)
    finally:
        client.close()

def test_partial_install_stops_original_retry(monkeypatch):
    from agent import chat_completion_helpers as helpers
    key = "fixture-key"
    digest = hashlib.sha256(key.encode()).hexdigest()
    route = ResolvedRoute("anthropic", "fixture-model", "https://api.anthropic.com", "anthropic_messages", None, key, digest, "{}")
    pin = RuntimePin("fixture", "primary", "primary-model", "https://primary.invalid", "chat_completions", None, digest, True, fallback_routes=(route,))
    child = SimpleNamespace(_fallback_chain=[route.native_entry()], _fallback_index=0, _delegation_runtime_pin=pin, model=pin.model, provider=pin.provider, base_url=pin.base_url, api_key=key)
    monkeypatch.setattr("agent.fallback_cooldown._arm_rate_limit_cooldown", lambda *a: None)
    monkeypatch.setattr(helpers, "_should_skip_fallback_candidate", lambda *a: False)
    monkeypatch.setattr(helpers, "_rebind_fallback_credential_pool", lambda *a, **kw: None)
    def fail(child, *args):
        child.api_key = "partial-fixture-key"
        raise ValueError("synthetic partial install")
    monkeypatch.setattr("agent.client_lifecycle._swap_fallback_clients", fail)
    child._try_activate_fallback = lambda *a: pytest.fail("must not continue original retry/sleep")
    with pytest.raises(ValueError, match="named subagent fallback installation failed"):
        helpers.try_activate_fallback(child)


@pytest.mark.parametrize("route_kind", ["primary", "fallback", "unpinned"])
def test_request_client_keeps_pinned_oauth_authority(monkeypatch, route_kind):
    from agent.anthropic_adapter import build_anthropic_client
    from agent.client_lifecycle import ClientLifecycleMixin, _swap_fallback_clients
    from tools.custom_subagent_fallbacks import frozen_fallback_client
    from unittest.mock import Mock

    key = "sk-ant-oat01-" + "fixture-launch"
    ambient = "sk-ant-oat01-" + "fixture-ambient"
    url = "https://api.anthropic.com"
    digest = hashlib.sha256(key.encode()).hexdigest()
    route = ResolvedRoute("anthropic", "fixture-model", url, "anthropic_messages", None, key, digest, "{}")
    fallback = route_kind == "fallback"
    pin = RuntimePin("fixture", "primary" if fallback else route.provider,
                     "primary-model" if fallback else route.model,
                     "https://primary.invalid" if fallback else url,
                     "chat_completions" if fallback else route.api_mode,
                     None, digest, True, fallback_routes=(route,) if fallback else ())
    child = ClientLifecycleMixin()
    child.provider, child.model, child.base_url = route.provider, route.model, url
    child.api_mode, child.api_key, child.request_overrides = route.api_mode, key, {}
    child._delegation_runtime_pin = None if route_kind == "unpinned" else pin
    if fallback:
        _swap_fallback_clients(child, frozen_fallback_client(pin, route.native_entry()), route.provider, route.model, url, route.api_mode)
    else:
        child._anthropic_api_key, child._anthropic_base_url = key, url
        child._anthropic_client = build_anthropic_client(key, url)
    original = child._anthropic_client
    resolver = Mock(return_value=ambient)
    monkeypatch.setattr("agent.anthropic_credentials.resolve_anthropic_token", resolver)
    client = None
    try:
        client = child._create_request_anthropic_client(reason="pin-regression")
        if route_kind == "unpinned":
            resolver.assert_called_once_with()
            assert client.auth_token == ambient
            assert child._anthropic_client is not original
        else:
            resolver.assert_not_called()
            assert client.auth_token == key
            assert child._anthropic_client is original
            assert child._anthropic_api_key == key
            pin.validate_request(child, {"model": route.model}, client=client)
            assert pin._credential_digest == digest
    finally:
        if client is not None:
            client.close()
        child._anthropic_client.close()
