"""Reaction exposure follows gateway selections and registered platform identities."""

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_context import clear_session_vars, set_session_vars
from model_tools import get_tool_definitions
from tools import react_to_message_tool as reactions


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
@pytest.mark.parametrize("platform_name", ["telegram", "photon", "buzz", "runtime-reactions"])
def test_gateway_reaction_selection(config, override, expected_enabled, expected_disabled, monkeypatch, platform_name):
    from gateway import platform_registry as platforms

    if platform_name != "telegram":
        registry = platforms.PlatformRegistry()
        monkeypatch.setattr(platforms, "platform_registry", registry)
        registry.register(platforms.PlatformEntry(
            name=platform_name, label=platform_name,
            adapter_factory=lambda config: None, check_fn=lambda: True,
        ))
        config = {**config, "platform_toolsets": {
            platform_name: config["platform_toolsets"]["telegram"],
        }} if "platform_toolsets" in config else config
    monkeypatch.setattr(reactions.desktop_ui, "user_enabled", lambda *a, **kw: False)
    runner = object.__new__(GatewayRunner)
    runner._adapter_for_source = lambda source: SimpleNamespace(toolsets_for_source=lambda source: override)
    source = SessionSource(platform=Platform(platform_name), chat_id="chat")

    enabled, disabled = runner._resolve_turn_toolsets(config, source, platform_name)

    assert disabled == expected_disabled
    tokens = set_session_vars(platform=source.platform.value)
    try:
        tools = get_tool_definitions(
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
            quiet_mode=True, skip_tool_search_assembly=True,
        )
        assert ("react_to_message" in {tool["function"]["name"] for tool in tools}) is expected_enabled
    finally:
        clear_session_vars(tokens)


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
    runner._adapter_for_source = lambda source: None
    source = SessionSource(platform=Platform(platform_name), chat_id="chat")
    config = {} if selection is None else {"platform_toolsets": {platform_name: selection}}

    enabled, disabled = runner._resolve_turn_toolsets(config, source, platform_name)

    assert "reactions" not in (disabled or [])
    tokens = set_session_vars(platform=source.platform.value)
    try:
        tools = get_tool_definitions(
            enabled_toolsets=enabled, disabled_toolsets=disabled,
            quiet_mode=True, skip_tool_search_assembly=True,
        )
        assert "react_to_message" in {tool["function"]["name"] for tool in tools}
    finally:
        clear_session_vars(tokens)