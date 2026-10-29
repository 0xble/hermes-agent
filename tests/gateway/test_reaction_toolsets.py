"""Reaction exposure follows gateway selections and registered platform identities."""

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from model_tools import get_tool_definitions
from tools import react_to_message_tool as reactions
from tools.react_to_message_tool import reaction_disabled_toolsets


@pytest.mark.parametrize(
    "config,override,expected_enabled,expected_disabled",
    [
        ({}, None, True, None),
        ({"platform_toolsets": {"telegram": ["web"]}}, None, False, None),
        ({"platform_toolsets": {"telegram": []}}, None, False, None),
        ({"agent": {"disabled_toolsets": ["reactions"]}}, None, False, ["reactions"]),
        ({"agent": {"disabled_toolsets": "['reactions']"}}, None, False, ["reactions"]),
        ({"platform_toolsets": {"telegram": ["reactions"]},
          "agent": {"disabled_toolsets": ["reactions"]}}, None, False, ["reactions"]),
        ({}, ["web"], False, None),
        ({"platform_toolsets": {"telegram": ["web"]}}, ["reactions"], True, None),
        ({"agent": {"disabled_toolsets": ["reactions"]}}, ["reactions"], False, ["reactions"]),
    ],
    ids=["default", "web-pin", "empty-pin", "disabled", "string-disabled", "disabled-pin",
         "adapter-web", "adapter-reactions", "disabled-adapter-reactions"],
)
def test_gateway_reaction_selection(config, override, expected_enabled, expected_disabled, monkeypatch):
    monkeypatch.setattr(reactions.desktop_ui, "user_enabled", lambda *a, **kw: False)
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda source: SimpleNamespace(toolsets_for_source=lambda source: override)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat")

    enabled, disabled = runner._resolve_turn_toolsets(config, source, "telegram")

    assert ("reactions" in enabled) is expected_enabled
    assert disabled == expected_disabled
    if config == {"platform_toolsets": {"telegram": []}}:
        # Non-configurable platform-native tools survive checklist pins. The
        # empty selection still excludes reactions and configurable toolsets.
        assert set(enabled) == {"session_state", "telegram_topic"}
    tools = get_tool_definitions(
        enabled_toolsets=enabled,
        disabled_toolsets=reaction_disabled_toolsets(disabled, source.platform.value),
        quiet_mode=True, skip_tool_search_assembly=True,
    )
    assert ("react_to_message" in {tool["function"]["name"] for tool in tools}) is expected_enabled


@pytest.mark.parametrize("platform_name", ["photon", "buzz", "runtime-reactions"])
@pytest.mark.parametrize("selection", [None, ["reactions"]], ids=["default", "explicit"])
def test_plugin_gateway_exposes_reaction_without_desktop_opt_in(platform_name, selection, monkeypatch):
    from gateway import platform_registry as platforms

    # Use the real registration pathway, including a name absent from bundled plugins.
    registry = platforms.PlatformRegistry()
    monkeypatch.setattr(platforms, "platform_registry", registry)
    registry.register(platforms.PlatformEntry(
        name=platform_name, label=platform_name,
        adapter_factory=lambda config: None, check_fn=lambda: True,
    ))
    monkeypatch.setattr(reactions.desktop_ui, "user_enabled", lambda *a, **kw: False)
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda source: None
    source = SessionSource(platform=Platform(platform_name), chat_id="chat")
    config = {} if selection is None else {"platform_toolsets": {platform_name: selection}}

    enabled, disabled = runner._resolve_turn_toolsets(config, source, platform_name)
    disabled = reaction_disabled_toolsets(disabled, source.platform.value)

    assert "reactions" not in (disabled or [])
    tools = get_tool_definitions(
        enabled_toolsets=enabled, disabled_toolsets=disabled,
        quiet_mode=True, skip_tool_search_assembly=True,
    )
    assert "react_to_message" in {tool["function"]["name"] for tool in tools}
