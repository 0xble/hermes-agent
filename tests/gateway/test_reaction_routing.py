"""Reaction delivery uses the current execution's transport, workspace and inbound id."""
import asyncio
import json
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.session_context import (
    current_message_reaction_acknowledged, reaction_acknowledgement, scoped_turn_message,
)
from gateway.turn_context import TurnContext
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from plugins.platforms.slack.adapter import SlackAdapter
from tools.registry import discover_builtin_tools, registry
from tools.send_message_tool import send_message_tool


@pytest.fixture(autouse=True)
def _registered_tools():
    discover_builtin_tools()


def _slack():
    adapter = object.__new__(SlackAdapter)
    transports = {team: AsyncMock(return_value={"ok": True}) for team in ("T1", "T2")}
    adapter._team_clients = {team: SimpleNamespace(reactions_add=fn) for team, fn in transports.items()}
    adapter._channel_team = {"C123": "T1"}  # Explicit inbound workspace must beat this cache.
    adapter._app = SimpleNamespace(client=SimpleNamespace(reactions_add=AsyncMock()))
    return adapter, transports


def _turn(runner, source, inbound):
    ctx = TurnContext(source=source, message="thanks", history=[], session_id="sid", session_key="key",
                      event_message_id="opening-message", inbound_message_id=inbound)
    ctx.approval_session_key = "key"
    turn = TurnRunner(runner, ctx)
    turn._wire_attended_turn_router = lambda *_: None
    turn._native_image_run_message = lambda: "thanks"
    turn._clear_session_approval = lambda *_: None
    turn._tool_progress_callbacks_for_turn = lambda *_: (None, None)
    return turn


@pytest.mark.asyncio
@pytest.mark.parametrize("action,outcome", [
    (action, outcome)
    for action in ("react_to_message", "send_message")
    for outcome in ("delivered", "failed", "other-target", "none", "offline", "unknown-workspace")
    if not (action == "react_to_message" and outcome == "other-target")
])
@pytest.mark.parametrize("final_text", ["", "NO_REPLY", "[SILENT]"])
async def test_actual_turn_route_isolates_accounts_workspaces_and_queued_targets(monkeypatch, tmp_path, action, outcome, final_text):
    primary, primary_calls = _slack()
    secondary, secondary_calls = _slack()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {Platform.SLACK: primary}
    runner._profile_adapters = {"sec": {Platform.SLACK: secondary}, "offline": {}}
    runner._primary_profile_name = "default"
    runner._gateway_loop = asyncio.get_running_loop()
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    # Runtime and receiving-bot profiles intentionally differ. Use real provenance resolution.
    source = SessionSource(platform=Platform.SLACK, profile="satellite", chat_id="C123",
                           scope_id="T2", user_id="human")
    if outcome == "offline":
        source.profile = "offline"
    else:
        source._transport_adapter_ref = weakref.ref(secondary)
    if outcome == "failed":
        secondary_calls["T2"].return_value = {"ok": False}
    if outcome == "unknown-workspace":
        source.scope_id = "T3"
    if action == "send_message":
        # Target authorization is independently covered. Keep transport selection and dispatch real.
        monkeypatch.setattr("tools.send_message_tool._authorize_relay_target", lambda *_: None)

    class Agent:
        def run_conversation(self, *args, **kwargs):
            if outcome != "none":
                if action == "send_message":
                    args = {"action": "react", "target": "slack:C123", "emoji": "👍",
                            "message_id": "old-message" if outcome == "other-target" else "queued-message"}
                else:
                    args = {"emoji": "👍", **({"message_id": "old-message"} if outcome == "other-target" else {})}
                result = json.loads(registry.dispatch(action, args) if action == "react_to_message" else send_message_tool(args))
                assert bool(result.get("success")) is (outcome not in {"failed", "offline", "unknown-workspace"}), result
            return {"final_response": final_text, "api_calls": 1, "completed": True, "messages": []}

    turn = _turn(runner, source, "queued-message")
    opening = SessionSource(platform=Platform.SLACK, profile="default", chat_id="old-chat",
                            scope_id="T1", user_id="old-human")
    with scoped_turn_message(opening, "opening-message"), reaction_acknowledgement():
        result = await asyncio.to_thread(turn._run_conversation_with_approval, Agent(), [], None, "thanks", None)
        assert not current_message_reaction_acknowledged()
    assert result["acknowledged_by_reaction"] is (outcome == "delivered")
    from tests.gateway.test_gateway_silence_tokens import _event, _runner
    delivery = _runner(monkeypatch, tmp_path)
    delivery._run_agent = AsyncMock(return_value=result)
    event = _event()
    event.source = source
    response = await delivery._handle_message_with_agent(event, source, "key", 1)
    if outcome == "delivered":
        assert not response
        assert delivery._run_agent.await_count == 1
    else:
        assert response and response != final_text
    for fn in primary_calls.values():
        fn.assert_not_awaited()
    secondary_calls["T1"].assert_not_awaited()
    primary._app.client.reactions_add.assert_not_awaited()
    secondary._app.client.reactions_add.assert_not_awaited()
    if outcome in {"none", "offline", "unknown-workspace"}:
        secondary_calls["T2"].assert_not_awaited()
    else:
        secondary_calls["T2"].assert_awaited_once_with(
            channel="C123", timestamp="old-message" if outcome == "other-target" else "queued-message",
            name="thumbsup")


@pytest.mark.asyncio
async def test_actual_delivery_receipt_does_not_answer_another_workspace_or_profile(monkeypatch, tmp_path):
    adapter, transports = _slack()
    runner = SimpleNamespace(adapters={Platform.SLACK: adapter}, _gateway_loop=asyncio.get_running_loop())
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    source = SessionSource(platform=Platform.SLACK, chat_id="C123", scope_id="T1", user_id="human")
    # This bare route intentionally uses profile lookup without a full TurnRunner.
    with reaction_acknowledgement():
        from gateway.session_context import clear_session_vars, set_session_vars
        tokens = set_session_vars(platform="slack", chat_id="C123", message_id="same-message", scope_id="T1")
        try:
            assert json.loads(await asyncio.to_thread(registry.dispatch, "react_to_message", {"emoji": "👍"}))["success"]
            assert current_message_reaction_acknowledged()
            source.scope_id = "T2"
            with scoped_turn_message(source, "same-message"):
                assert not current_message_reaction_acknowledged()
            token = set_hermes_home_override(tmp_path / "other-profile")
            try:
                assert not current_message_reaction_acknowledged()
            finally:
                reset_hermes_home_override(token)
            assert current_message_reaction_acknowledged()
        finally:
            clear_session_vars(tokens)
    transports["T1"].assert_awaited_once()
    transports["T2"].assert_not_awaited()
