"""Adapter normalization must apply at the physical fallback client boundary."""
import hashlib
from types import SimpleNamespace

import pytest

from agent.anthropic_adapter import build_anthropic_client
from tools.custom_subagents import ResolvedRoute, RuntimePin


def pin_and_child(mode="anthropic_messages", base="https://example.invalid/tenant/v1"):
    key = "fixture-static-key"
    digest = hashlib.sha256(key.encode()).hexdigest()
    route = ResolvedRoute("fixture-provider", "fallback-model", base, mode, None,
                          api_key=key, credential_digest=digest)
    pin = RuntimePin("fixture", "openai", "primary-model", "https://primary.invalid/v1",
                     "chat_completions", None, digest, True, fallback_routes=(route,))
    child = SimpleNamespace(provider=route.provider, model=route.model, base_url=base,
                            api_mode=mode, api_key=key, request_overrides={})
    return pin, child


@pytest.mark.parametrize("base", ["https://example.invalid/v1", "https://example.invalid/tenant/v1"])
def test_real_anthropic_fallback_accepts_adapter_root_and_rejects_changed_path(base):
    pin, child = pin_and_child(base=base)
    client = build_anthropic_client(child.api_key, child.base_url)
    try:
        pin.validate_request(child, {"model": child.model}, client=client, final_request=True)
        client.base_url = "https://example.invalid/other/"
        with pytest.raises(ValueError, match="route"):
            pin.validate_request(child, {"model": child.model}, client=client, final_request=True)
    finally:
        client.close()


def test_fallback_frozen_route_and_credential_stay_strict():
    pin, child = pin_and_child()
    client = build_anthropic_client(child.api_key, child.base_url)
    try:
        child.base_url = "https://example.invalid/tenant"
        with pytest.raises(ValueError, match="route"):
            pin.validate_request(child, {"model": child.model}, client=client, final_request=True)
        child.base_url = pin.fallback_routes[0].base_url
        client.api_key = "other-key"
        with pytest.raises(ValueError, match="credential|authentication"):
            pin.validate_request(child, {"model": child.model}, client=client, final_request=True)
    finally:
        client.close()


def test_non_anthropic_fallback_does_not_strip_v1():
    from openai import OpenAI
    pin, child = pin_and_child("chat_completions")
    with OpenAI(api_key=child.api_key, base_url="https://example.invalid/tenant") as client:
        with pytest.raises(ValueError, match="route"):
            pin.validate_request(child, {"model": child.model}, client=client, final_request=True)


def test_fallback_client_without_inspectable_url_is_refused():
    pin, child = pin_and_child()
    client = SimpleNamespace(api_key=child.api_key)
    with pytest.raises(ValueError, match="route"):
        pin.validate_request(child, {"model": child.model}, client=client, final_request=True)
