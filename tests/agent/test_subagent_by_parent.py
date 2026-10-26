"""Plugin lifecycle launches share ordinary delegation's parent-dependent routes."""

from unittest.mock import MagicMock, patch

import pytest

from agent import subagent_lifecycle as lifecycle
from tests.tools.test_delegate_by_parent import (
    HIGH,
    MEDIUM,
    PARENT_CHAIN,
    SOL,
    VENICE,
    _authored,
    _parent,
    _runtime,
    config_file as _config_file,
)


config_file = _config_file


def _launch(parent, **kwargs):
    service = lifecycle.SubagentLifecycleService(lambda: parent)
    with (
        patch("run_agent.AIAgent", return_value=MagicMock()) as agent,
        patch.object(lifecycle._EXECUTOR, "submit"),
    ):
        handle = service.launch(
            lifecycle.SubagentLaunchRequest(goal="Check plugin route", **kwargs)
        )
    with lifecycle._REGISTRY.lock:
        lifecycle._REGISTRY.records.pop(handle.subagent_id)
    return agent.call_args.kwargs


def test_lifecycle_launch_reaches_the_shared_fork_builder_with_selected_reasoning(config_file):
    from tools import delegate_tool

    raw = _authored()
    raw["delegation"]["max_iterations"] = 19
    config_file(raw)
    parent = _parent()
    parent.session_id = "parent-plugin"
    with patch.object(delegate_tool, "_build_child_agent", wraps=delegate_tool._build_child_agent) as builder:
        built = _launch(parent)
    assert builder.call_count == 1
    selected = builder.call_args.kwargs["reasoning_cfg"]
    assert "provider" not in selected and "model" not in selected
    assert selected["max_iterations"] == 19
    assert built["reasoning_config"] == MEDIUM


def test_lifecycle_inherits_venice_route_chain_and_reasoning(config_file):
    parent = _parent()
    parent.session_id = "parent-plugin"
    kwargs = _launch(parent)
    assert (kwargs["model"], kwargs["provider"], kwargs["base_url"]) == (
        VENICE,
        parent.provider,
        parent.base_url,
    )
    assert kwargs["fallback_model"] == PARENT_CHAIN
    assert kwargs["reasoning_config"] == MEDIUM
    assert kwargs["request_overrides"] == parent.request_overrides


@pytest.mark.parametrize("model", [SOL, "claude-opus-4.8"])
def test_lifecycle_unmatched_parents_unchanged(config_file, model):
    parent = _parent(model)
    parent.session_id = "parent-plugin"
    routed = _launch(parent)
    raw = _authored()
    raw["delegation"].pop("by_parent")
    config_file(raw)
    original = _launch(parent)
    assert _runtime(routed) == _runtime(original)
    assert routed["model"] == SOL
    assert routed["reasoning_config"] == HIGH


@pytest.mark.parametrize("provider", [None, "custom:explicit"])
def test_explicit_lifecycle_launch_overrides_win(config_file, provider):
    parent = _parent()
    parent.session_id = "parent-plugin"
    runtime = {
        "provider": "custom",
        "base_url": "https://explicit.test/v1",
        "api_key": "test-key",
    }
    with patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime
    ):
        kwargs = _launch(parent, provider=provider, model="explicit-model")
    assert kwargs["model"] == "explicit-model"
    assert kwargs["provider"] == (provider or parent.provider)
    assert kwargs["base_url"] == (runtime["base_url"] if provider else parent.base_url)
    assert kwargs["reasoning_config"] == (HIGH if provider else MEDIUM)
