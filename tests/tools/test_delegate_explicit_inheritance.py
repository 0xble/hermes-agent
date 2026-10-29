"""Delegated children inherit the parent's *explicit* reasoning pick and its Fast mode.

Contracts:
- An explicit session reasoning pick (``agent.reasoning_override`` equal to the live level)
  beats ``delegation.reasoning_effort``; a configured parent level does not.
- The marker is honoured only while it still matches ``reasoning_config`` (a model switch or
  fallback that re-resolves from config retires it without clearing it).
- With ``delegation.inherit_service_tier``, the parent's Fast *mode* crosses to the child even
  when ``delegation.provider`` pins another route; the wire field is re-derived for the child's
  route and nothing is sent on a route that has no fast mode.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tools import delegate_tool_config as config

XHIGH = {"enabled": True, "effort": "xhigh"}
HIGH = {"enabled": True, "effort": "high"}
MEDIUM = {"enabled": True, "effort": "medium"}


def _runtime_parent(**attrs):
    parent = MagicMock()
    parent.base_url = "https://api.openai.com/v1"
    parent.api_key = "***"
    parent.provider = "openai"
    parent.api_mode = "chat_completions"
    parent.model = "gpt-5.4"
    parent.reasoning_override = None
    for name, value in attrs.items():
        setattr(parent, name, value)
    return parent


def _child_reasoning(parent, delegation_cfg):
    return config._resolve_child_runtime(
        parent, delegation_cfg, "***", model=None, override_provider=None, override_base_url=None,
        override_api_key=None, override_api_mode=None, override_acp_command=None, override_acp_args=None,
    )["reasoning_config"]


@pytest.mark.parametrize(
    ("parent_level", "override", "delegation_effort", "expected"),
    [
        (XHIGH, XHIGH, "medium", XHIGH),  # explicit pick beats the delegation default
        (HIGH, None, "medium", MEDIUM),  # configured parent level does not
        (HIGH, None, "", HIGH),  # no delegation default: parent level, as before
        (XHIGH, XHIGH, "", XHIGH),
        # Stale marker: a switch re-resolved the level; the delegation default applies again.
        (HIGH, XHIGH, "medium", MEDIUM),
        # An explicit "none" (thinking off) is a real choice and crosses to the child.
        ({"enabled": False}, {"enabled": False}, "high", {"enabled": False}),
    ],
)
def test_child_reasoning_precedence(parent_level, override, delegation_effort, expected):
    parent = _runtime_parent(reasoning_config=parent_level, reasoning_override=override)
    assert _child_reasoning(parent, {"reasoning_effort": delegation_effort}) == expected


def test_child_reasoning_is_a_copy_not_the_parents_dict():
    parent = _runtime_parent(reasoning_config=dict(XHIGH), reasoning_override=dict(XHIGH))
    child = _child_reasoning(parent, {"reasoning_effort": "medium"})
    child["effort"] = "low"
    assert parent.reasoning_config == XHIGH


def _fast_parent(mode, **attrs):
    return SimpleNamespace(
        model="gpt-5.4", provider="openai", base_url="https://api.openai.com/v1",
        service_tier=mode, request_overrides={}, _fast_until=0, **attrs,
    )


@pytest.mark.parametrize("mode", ["priority", "auto", "cold"])
def test_fast_mode_crosses_verbatim_when_enabled(mode):
    with patch.object(config, "_cfg", return_value={"inherit_service_tier": True}):
        assert config._resolve_child_service_tier(_fast_parent(mode), None) == mode


@pytest.mark.parametrize(
    ("cfg", "mode", "explicit"),
    [
        ({}, "priority", None),  # inheritance off by default
        ({"inherit_service_tier": True}, None, None),  # parent not in Fast
        ({"inherit_service_tier": True}, "priority", {"service_tier": "normal"}),  # explicit wins
    ],
)
def test_fast_mode_not_inherited(cfg, mode, explicit):
    with patch.object(config, "_cfg", return_value=cfg):
        assert config._resolve_child_service_tier(_fast_parent(mode), explicit) is None


def test_pinned_provider_still_inherits_fast_rederived_for_child_route():
    """``delegation.provider`` pins a different route: the mode crosses, the parent's wire field does not."""
    parent = _fast_parent("priority")
    parent.request_overrides = {"service_tier": "priority"}
    with patch.object(config, "_cfg", return_value={"inherit_service_tier": True}):
        result = config._resolve_child_request_overrides(
            parent, child_model="claude-opus-4.8", child_provider="anthropic",
            child_base_url="https://api.anthropic.com", explicit_overrides=None,
            inherit_parent_route=False,
        )
    assert result is not None and result.get("speed") == "fast"
    assert "service_tier" not in result


def test_route_without_fast_mode_gets_no_fast_fields():
    """A proxy route has no fast parameters: the child sends none (no surprise billing tier)."""
    with patch.object(config, "_cfg", return_value={"inherit_service_tier": True}):
        result = config._resolve_child_request_overrides(
            _fast_parent("priority"), child_model="gpt-5.4", child_provider="custom:codex-proxy",
            child_base_url="http://127.0.0.1:8317/v1", explicit_overrides=None,
            inherit_parent_route=False,
        )
    assert not any(key in (result or {}) for key in ("service_tier", "speed"))


def test_bounded_mode_is_not_pinned_as_static_fields():
    """``auto``/``cold`` stay bounded in the child (its own window), never pinned request fields."""
    with patch.object(config, "_cfg", return_value={"inherit_service_tier": True}):
        result = config._resolve_child_request_overrides(
            _fast_parent("auto"), child_model="gpt-5.4", child_provider="openai",
            child_base_url="https://api.openai.com/v1", explicit_overrides=None,
            inherit_parent_route=True,
        )
    assert not any(key in (result or {}) for key in ("service_tier", "speed"))


@patch("tools.delegate_tool._load_config")
@patch("run_agent.AIAgent")
def test_built_child_receives_explicit_reasoning_and_fast_mode(MockAgent, mock_cfg):
    """End to end through _build_child_agent: the child is constructed with both inherited settings."""
    from tools.delegate_tool import _build_child_agent
    from tests.tools.test_delegate import _make_mock_parent

    mock_cfg.return_value = {"max_iterations": 50, "reasoning_effort": "medium", "inherit_service_tier": True}
    MockAgent.return_value = MagicMock()
    parent = _make_mock_parent()
    parent.reasoning_config = dict(XHIGH)
    parent.reasoning_override = dict(XHIGH)
    parent.service_tier = "priority"
    parent.request_overrides = {}
    with patch.object(config, "_cfg", return_value=mock_cfg.return_value):
        _build_child_agent(
            task_index=0, goal="test", context=None, toolsets=None, model=None,
            max_iterations=50, parent_agent=parent, task_count=1,
        )
    kwargs = MockAgent.call_args[1]
    assert kwargs["reasoning_config"] == XHIGH
    assert kwargs["service_tier"] == "priority"


@patch("tools.delegate_tool._load_config")
@patch("run_agent.AIAgent")
def test_explicit_pick_stays_explicit_for_grandchildren(MockAgent, mock_cfg):
    """An orchestrator child re-marks the inherited pick, so its own children keep it too."""
    from tools.delegate_tool import _build_child_agent
    from tests.tools.test_delegate import _make_mock_parent

    mock_cfg.return_value = {"max_iterations": 50, "reasoning_effort": "medium"}
    child_agent = MagicMock()
    child_agent.reasoning_config = dict(XHIGH)
    MockAgent.return_value = child_agent

    parent = _make_mock_parent()
    parent.reasoning_config = dict(XHIGH)
    parent.reasoning_override = dict(XHIGH)
    child = _build_child_agent(
        task_index=0, goal="test", context=None, toolsets=None, model=None,
        max_iterations=50, parent_agent=parent, task_count=1,
    )
    assert child.reasoning_override == XHIGH

    # A configured (non-explicit) parent level does not mark the child.
    parent.reasoning_override = None
    child_agent.reasoning_override = None
    child = _build_child_agent(
        task_index=0, goal="test", context=None, toolsets=None, model=None,
        max_iterations=50, parent_agent=parent, task_count=1,
    )
    assert child.reasoning_override is None
