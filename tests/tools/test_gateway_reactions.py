"""Registered gateway reactions keep routing, event-loop and acknowledgement ownership."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.session_context import clear_session_vars, reaction_acknowledgement, set_session_vars
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
        with reaction_acknowledgement() as receipts:
            result = json.loads(await asyncio.to_thread(registry.dispatch, "react_to_message", {"emoji": "👍"}))
            assert bool(result.get("success")) is delivered
            assert bool(receipts) is delivered
            from gateway.session_context import current_message_reaction_acknowledged
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


def test_gateway_reaction_requirements_do_not_leak_to_desktop(monkeypatch):
    from tools.react_to_message_tool import reaction_disabled_toolsets

    monkeypatch.setattr(reactions.desktop_ui, "user_enabled", lambda *a, **kw: False)
    assert registry.is_toolset_available("reactions")
    assert "reactions" not in (reaction_disabled_toolsets([], "telegram") or [])
    for platform in ("desktop", "cli", "local", "cron", None):
        assert "reactions" in reaction_disabled_toolsets([], platform)
    from model_tools import get_tool_definitions
    tools = get_tool_definitions(enabled_toolsets=["hermes-telegram"],
                                 disabled_toolsets=reaction_disabled_toolsets([], "telegram"),
                                 quiet_mode=True, skip_tool_search_assembly=True)
    assert "react_to_message" in {tool["function"]["name"] for tool in tools}
    monkeypatch.setattr(reactions.desktop_ui, "user_enabled", lambda *a, **kw: True)
    assert "reactions" not in (reaction_disabled_toolsets([], "desktop") or [])
