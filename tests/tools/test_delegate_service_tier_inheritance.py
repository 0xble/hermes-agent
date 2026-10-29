from types import SimpleNamespace
from unittest.mock import patch

from tools import delegate_tool_config as config


def _parent(*, fast=True):
    return SimpleNamespace(
        model="gpt-5.4",
        provider="openai",
        base_url="https://api.openai.com/v1",
        service_tier="priority" if fast else None,
        request_overrides={
            "service_tier": "priority" if fast else "normal",
            "extra_body": {"provider": {"sort": "throughput"}},
        },
        _fast_until=0,
    )


def test_fast_is_not_inherited_by_default_but_unrelated_overrides_are():
    with patch.object(config, "_cfg", return_value={}):
        result = config._resolve_child_request_overrides(
            _parent(),
            child_model="gpt-5.4",
            child_provider="openai",
            child_base_url="https://api.openai.com/v1",
            explicit_overrides=None,
            inherit_parent_route=True,
        )

    assert result == {"extra_body": {"provider": {"sort": "throughput"}}}


def test_enabled_inheritance_rederives_fast_field_for_child_route():
    with patch.object(config, "_cfg", return_value={"inherit_service_tier": True}):
        result = config._resolve_child_request_overrides(
            _parent(),
            child_model="gpt-5.4",
            child_provider="openai",
            child_base_url="https://api.openai.com/v1",
            explicit_overrides=None,
            inherit_parent_route=True,
        )

    assert result["service_tier"] == "priority"
    assert result["extra_body"] == {"provider": {"sort": "throughput"}}


def test_normal_parent_does_not_inherit_priority_override():
    parent = _parent(fast=False)
    parent.request_overrides["service_tier"] = "priority"
    with patch.object(config, "_cfg", return_value={"inherit_service_tier": True}):
        result = config._resolve_child_request_overrides(
            parent,
            child_model="gpt-5.4",
            child_provider="openai",
            child_base_url="https://api.openai.com/v1",
            explicit_overrides=None,
            inherit_parent_route=True,
        )

    assert result is not None
    assert "service_tier" not in result


def test_enabled_inheritance_uses_child_wire_field():
    with patch.object(config, "_cfg", return_value={"inherit_service_tier": True}):
        result = config._resolve_child_request_overrides(
            _parent(),
            child_model="claude-opus-4.8",
            child_provider="anthropic",
            child_base_url="https://api.anthropic.com",
            explicit_overrides=None,
            inherit_parent_route=True,
        )

    assert result["speed"] == "fast"
    assert "service_tier" not in result


def test_explicit_child_fast_override_wins_over_inherited_parent():
    with patch.object(config, "_cfg", return_value={"inherit_service_tier": True}):
        result = config._resolve_child_request_overrides(
            _parent(),
            child_model="gpt-5.4",
            child_provider="openai",
            child_base_url="https://api.openai.com/v1",
            explicit_overrides={"service_tier": "normal"},
            inherit_parent_route=True,
        )

    assert result["service_tier"] == "normal"
    assert "speed" not in result
