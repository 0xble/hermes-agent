"""Ultrafast inheritance is gated on the child's route, never a paid fallback."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import hermes_yaml as yaml
import run_agent

from tools import delegate_tool_config as config

PROXY = "http://127.0.0.1:8317/v1"


@pytest.fixture(autouse=True)
def inheritance(monkeypatch):
    monkeypatch.setattr(config, "_inherit_service_tier", lambda: True)
    from hermes_constants import get_hermes_home

    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump({"providers": {
        "openai-proxy": {"base_url": PROXY, "api_mode": "codex_responses",
                        "capabilities": {"fast_mode": True}},
        "claude-proxy": {"base_url": PROXY, "api_mode": "anthropic_messages"},
    }}))


def _parent():
    return SimpleNamespace(service_tier="ultrafast", request_overrides={
        "service_tier": "ultrafast", "extra_body": {"service_tier": "ultrafast", "keep": True},
    })


@pytest.mark.parametrize("model,provider,url,expected", [
    ("gpt-6.1-sol", "custom:openai-proxy", PROXY, "ultrafast"),
    ("gpt-6.1-sol", "custom", PROXY, "ultrafast"),
    ("gpt-6.1-sol", "openai", "https://api.openai.com/v1", "ultrafast"),
    ("gpt-5.4", "openai", "https://api.openai.com/v1", None),
    ("gpt-6.1-sol", "custom:openai-proxy", "http://127.0.0.1:9999/v1", None),
    ("gpt-6.1-sol", "openrouter", "https://openrouter.ai/api/v1", None),
    ("claude-opus-5-5", "custom:claude-proxy", PROXY, None),
    ("claude-opus-4.8", "anthropic", "https://api.anthropic.com", None),
])
@pytest.mark.parametrize("same_route", [True, False])
def test_ultrafast_uses_child_route(model, provider, url, expected, same_route):
    parent = _parent()
    route = dict(child_model=model, child_provider=provider, child_base_url=url)
    result = config._resolve_child_request_overrides(
        parent, **route, explicit_overrides=None, inherit_parent_route=same_route) or {}
    assert config._resolve_child_service_tier(parent, None, **route) == expected
    assert result.get("service_tier") == expected
    assert "speed" not in result
    assert result.get("extra_body") == ({"keep": True} if same_route else None)
    assert parent.request_overrides["extra_body"]["service_tier"] == "ultrafast"


@pytest.mark.parametrize("explicit", [
    {"service_tier": "priority"}, {"service_tier": None}, {"speed": "fast"},
    {"extra_body": {"service_tier": "default"}}, {"extra_body": {"speed": "fast"}},
])
def test_explicit_overrides_win(explicit):
    route = dict(child_model="gpt-6.1-sol", child_provider="custom:openai-proxy", child_base_url=PROXY)
    assert config._resolve_child_service_tier(_parent(), explicit, **route) is None
    result = config._resolve_child_request_overrides(
        _parent(), **route, explicit_overrides=explicit, inherit_parent_route=False)
    assert result == explicit


@pytest.mark.parametrize("offered", [True, False])
def test_account_gate_uses_child_credential(offered):
    route = dict(child_model="gpt-6.1-sol", child_provider="openai-codex",
                 child_base_url="https://chatgpt.com/backend-api/codex", child_api_key="child-token")
    with patch("agent.codex_service_tiers.ultrafast_offered", return_value=offered) as gate:
        result = config._resolve_child_request_overrides(
            _parent(), **route, explicit_overrides=None, inherit_parent_route=False)
    gate.assert_called_once_with("gpt-6.1-sol", provider="openai-codex",
                                 base_url=route["child_base_url"], access_token="child-token")
    assert (result or {}).get("service_tier") == ("ultrafast" if offered else None)


def test_disabled_inheritance_never_pins_ultrafast(monkeypatch):
    monkeypatch.setattr(config, "_inherit_service_tier", lambda: False)
    assert config._resolve_child_request_overrides(
        _parent(), child_model="gpt-6.1-sol", child_provider="custom:openai-proxy", child_base_url=PROXY,
        explicit_overrides=None, inherit_parent_route=False) is None


@pytest.mark.parametrize("offered", [True, False])
def test_built_child_gets_gated_mode_and_wire_fields(offered):
    from tools.delegate_tool import _build_child_agent
    from tests.tools.test_delegate import _make_mock_parent

    parent = _make_mock_parent()
    parent.service_tier = "ultrafast"
    parent.request_overrides = {}
    runtime = dict(model="gpt-6.1-sol", provider="custom:openai-proxy", base_url=PROXY,
                   api_key="child-token", api_mode="codex_responses")
    with patch.object(run_agent, "AIAgent") as agent, patch("tools.delegate_tool._load_config", return_value={}), \
         patch("tools.delegate_tool._resolve_child_runtime", return_value=runtime), \
         patch("agent.codex_service_tiers.ultrafast_offered", return_value=offered):
        agent.return_value = MagicMock()
        _build_child_agent(task_index=0, goal="test", context=None, toolsets=None, model=None,
                           max_iterations=2, parent_agent=parent, task_count=1)
    kwargs = agent.call_args.kwargs
    assert kwargs["service_tier"] == ("ultrafast" if offered else None)
    assert kwargs["request_overrides"] == ({"service_tier": "ultrafast"} if offered else {})
