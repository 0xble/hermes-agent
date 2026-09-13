"""Named fallback activation preserves frozen authority across SDK URL spelling."""
import hashlib

import pytest

from tests.run_agent.test_custom_subagent_runtime import make_child
from tools.custom_subagents import ResolvedRoute, RuntimePin


@pytest.fixture(autouse=True)
def codex_token(monkeypatch):
    monkeypatch.setattr("agent.auxiliary_client._read_codex_access_token", lambda: "fixture-token")


def advisor(make_child):
    child = make_child("high", "gpt-6-astra")
    route = ResolvedRoute(
        child.provider, child.model, child.base_url, child.api_mode, "high",
        child.api_key, hashlib.sha256(child.api_key.encode()).hexdigest(), "{}",
    )
    child.provider = child.requested_provider = "anthropic"
    child.model = "claude-fable-5-1"
    child.base_url = "https://api.anthropic.com"
    child.api_mode = "anthropic_messages"
    child.api_key = "fixture-primary-key"
    child._delegation_runtime_pin = RuntimePin(
        "advisor", child.provider, child.model, child.base_url, child.api_mode,
        "high", hashlib.sha256(child.api_key.encode()).hexdigest(), True,
        None, (route,), "{}",
    )
    child._fallback_chain = [{
        "provider": route.provider, "model": route.model, "base_url": route.base_url,
        "api_key": route.api_key, "api_mode": route.api_mode, "reasoning_effort": "high",
    }]
    child._fallback_index = 0
    return child, route


def test_named_codex_fallback_activates_and_builds_pinned_request(make_child):
    # Real resolver and OpenAI SDK: no network call is needed for client creation.
    child, route = advisor(make_child)
    assert child._try_activate_fallback()
    assert child.base_url == route.base_url
    assert str(child.client.base_url) == route.base_url + "/"
    kwargs = child._build_api_kwargs([{"role": "user", "content": "Advise"}])
    child._delegation_runtime_pin.validate_request(child, kwargs, client=child.client)
    assert kwargs["model"] == route.model
    assert kwargs["reasoning"]["effort"] == "high"
    assert child._delegation_runtime_pin.pinned_base_url_for(child) == route.base_url
    child.base_url = route.base_url + "/"
    child._delegation_runtime_pin.validate_request(child, kwargs, client=child.client)
    assert child._delegation_runtime_pin.pinned_base_url_for(child) == route.base_url
    assert child._delegation_route_transitions[-1]["to"] == {
        "provider": route.provider, "model": route.model,
    }


@pytest.mark.parametrize("field,value", [
    ("base_url", "https://other.invalid/backend-api/codex"),
    ("base_url", "http://chatgpt.com/backend-api/codex"),
    ("base_url", "https://chatgpt.com/backend-api/other"),
    ("base_url", "https://chatgpt.com/backend-api/codex//"),
    ("base_url", "https://chatgpt.com/backend-api/codex?account=other"),
    ("base_url", "https://chatgpt.com:8443/backend-api/codex"),
    ("client_base_url", "https://other.invalid/backend-api/codex"),
    ("client_base_url", "https://chatgpt.com/backend-api/codex//"),
    ("client_base_url", "https://chatgpt.com/backend-api/codex?account=other"),
    ("provider", "openai"), ("model", "other-model"),
    ("api_mode", "chat_completions"), ("api_key", "other-key"),
])
def test_named_fallback_rejects_changed_authority(make_child, monkeypatch, field, value):
    child, route = advisor(make_child)
    if field == "client_base_url":
        from tools import custom_subagent_fallbacks
        resolve = custom_subagent_fallbacks.frozen_fallback_client

        def changed_client(*args, **kwargs):
            client = resolve(*args, **kwargs)
            client.base_url = value
            return client

        monkeypatch.setattr(custom_subagent_fallbacks, "frozen_fallback_client", changed_client)
    elif field == "api_key":
        child._fallback_chain[0][field] = value
    else:
        child._fallback_chain[0][field] = value
    from agent.errors import NamedFallbackInstallationError
    with pytest.raises(NamedFallbackInstallationError, match="named subagent fallback installation failed"):
        child._try_activate_fallback()
    assert not getattr(child, "_delegation_route_transitions", [])
