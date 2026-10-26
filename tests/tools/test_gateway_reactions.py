"""Registered gateway reactions keep routing, event-loop and acknowledgement ownership."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.session_context import clear_session_vars, current_message_reaction_acknowledged, reaction_acknowledgement, set_session_vars
from tools import react_to_message_tool as reactions
from tools.registry import registry


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "telegram", "discord"])
@pytest.mark.parametrize("delivered", [True, False])
async def test_registered_reaction_runs_on_gateway_loop(monkeypatch, platform, delivered):
    loop = asyncio.get_running_loop()
    calls = []

    async def react(chat_id, message_id, emoji, **routing):
        assert asyncio.get_running_loop() is loop
        calls.append((chat_id, message_id, emoji, routing))
        return delivered

    adapter = SimpleNamespace(add_reaction=react)
    runner = SimpleNamespace(_gateway_loop=loop, adapters={Platform(platform): adapter})
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    tokens = set_session_vars(platform=platform, chat_id="chat", message_id="message", scope_id="workspace", user_id="sender")
    try:
        with reaction_acknowledgement():
            result = json.loads(await asyncio.to_thread(registry.dispatch, "react_to_message", {"emoji": "👍"}))
            assert bool(result.get("success")) is delivered
            assert current_message_reaction_acknowledged() is delivered
        assert len(calls) == 1
        assert calls[0][:3] == ("chat", "message", "👍")
        assert calls[0][3]["team_id"] == "workspace"
        assert calls[0][3]["user_id"] == "sender"
    finally:
        clear_session_vars(tokens)


def test_unsupported_platform_does_not_acknowledge(monkeypatch):
    from gateway.platforms.base import BasePlatformAdapter
    adapter = SimpleNamespace(add_reaction=BasePlatformAdapter.add_reaction.__get__(object()))
    runner = SimpleNamespace(adapters={Platform.EMAIL: adapter})
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    tokens = set_session_vars(platform="email", chat_id="chat", message_id="message")
    try:
        with reaction_acknowledgement() as receipts:
            result = json.loads(reactions.react_to_message_tool("👍"))
            assert "does not support" in result["error"]
            assert not receipts
    finally:
        clear_session_vars(tokens)


def test_gateway_reaction_error_does_not_acknowledge(monkeypatch):
    adapter = SimpleNamespace(add_reaction=AsyncMock(side_effect=RuntimeError("denied")))
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    tokens = set_session_vars(platform="telegram", chat_id="chat", message_id="message")
    try:
        with reaction_acknowledgement() as receipts:
            assert "denied" in json.loads(reactions.react_to_message_tool("👍"))["error"]
            assert not receipts
        assert "desktop-only" in reactions.react_to_message_tool("👍", messages_back=1)
    finally:
        clear_session_vars(tokens)


@pytest.mark.parametrize("platform", ["desktop", "cli", "local", "cron", "telegram", "slack"])
@pytest.mark.parametrize("desktop_enabled", [False, True])
def test_gateway_reaction_requirements_do_not_leak_to_desktop(monkeypatch, platform, desktop_enabled):
    from model_tools import get_tool_definitions
    monkeypatch.setattr(reactions.desktop_ui, "user_enabled", lambda *a, **kw: desktop_enabled)
    tokens = set_session_vars(platform=platform)
    try:
        tools = get_tool_definitions(enabled_toolsets=["reactions"], disabled_toolsets=[],
                                     quiet_mode=True, skip_tool_search_assembly=True)
        assert ("react_to_message" in {tool["function"]["name"] for tool in tools}) is (
            platform in ("telegram", "slack") or desktop_enabled)
    finally:
        clear_session_vars(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["registered", "public"])
async def test_profile_and_workspace_route_isolation(monkeypatch, tmp_path, action):
    from unittest.mock import AsyncMock
    from gateway import run as gateway_run
    from gateway.config import GatewayConfig, Platform
    from hermes_cli import profiles
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from plugins.platforms.slack.adapter import SlackAdapter
    from tools import send_message_tool as send_tool
    from tools.registry import discover_builtin_tools
    discover_builtin_tools()
    home = tmp_path / "hermes"
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: home / "profiles")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._primary_profile_name = "default"
    runner._gateway_loop = asyncio.get_running_loop()
    clients = {}
    adapters = {}
    for profile in ("default", "secondary"):
        adapter = object.__new__(SlackAdapter)
        # Colliding channel/message IDs across 2 workspaces must hit distinct clients.
        workspace_clients = {
            team: SimpleNamespace(reactions_add=AsyncMock(return_value={"ok": True}))
            for team in ("T1", "T2")
        }
        adapter._app = SimpleNamespace(client=SimpleNamespace(reactions_add=AsyncMock()))
        adapter._team_clients = workspace_clients
        adapter._channel_team = {"C-shared": "T1"}
        adapters[profile] = adapter
        clients[profile] = workspace_clients
    runner.adapters = {Platform.SLACK: adapters["default"]}
    runner._profile_adapters = {"secondary": {Platform.SLACK: adapters["secondary"]}}
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    monkeypatch.setattr(send_tool, "_authorize_relay_target", lambda *_a, **_kw: None)
    monkeypatch.setattr(send_tool, "_resolve_tool_target", lambda *_a, **_kw: ("slack", "C-shared", "thread", None))

    async def react(profile, workspace):
        scoped_home = home if profile == "default" else home / "profiles" / profile
        home_token = set_hermes_home_override(scoped_home)
        tokens = set_session_vars(platform="slack", chat_id="C-shared", message_id="trigger", scope_id=workspace, profile=profile)
        try:
            with reaction_acknowledgement():
                if action == "registered":
                    result = json.loads(await asyncio.to_thread(registry.dispatch, "react_to_message", {"emoji": "👍"}))
                else:
                    result = json.loads(await asyncio.to_thread(send_tool._handle_react, {
                        "target": "slack:C-shared", "message_id": "trigger", "emoji": "👍",
                    }))
                if profile == "missing":
                    assert result.get("error")
                    assert not current_message_reaction_acknowledged()
                else:
                    assert result["success"] is True
                    assert current_message_reaction_acknowledged()
        finally:
            clear_session_vars(tokens)
            reset_hermes_home_override(home_token)

    await asyncio.gather(*(react(profile, workspace) for profile in ("default", "secondary")
                           for workspace in ("T1", "T2")), react("missing", "T3"))
    for profile in ("default", "secondary"):
        for workspace in ("T1", "T2"):
            clients[profile][workspace].reactions_add.assert_awaited_once_with(
                channel="C-shared", timestamp="trigger", name="thumbsup")
        adapters[profile]._app.client.reactions_add.assert_not_called()
