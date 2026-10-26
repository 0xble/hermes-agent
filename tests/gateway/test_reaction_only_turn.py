"""Reaction-only delivery on the deployed gateway's existing finalization boundary."""
from unittest.mock import AsyncMock

import pytest

from tests.gateway.test_gateway_silence_tokens import _event, _runner, _source


@pytest.mark.parametrize("text", ["", "NO_REPLY", "[SILENT]", "A substantive reply"])
@pytest.mark.parametrize("acknowledged", [True, False])
@pytest.mark.asyncio
async def test_reaction_only_delivery(monkeypatch, tmp_path, text, acknowledged):
    runner = _runner(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(return_value={
        "final_response": text, "messages": [], "api_calls": 1,
        "completed": True, "acknowledged_by_reaction": acknowledged,
    })
    response = await runner._handle_message_with_agent(_event(), _source(), "key", 1)
    if text == "A substantive reply":
        assert response == text
    elif acknowledged or text:
        # Existing explicit silence markers keep their pre-backport semantics.
        assert not response
    else:
        assert "no response was generated" in response

@pytest.mark.parametrize("failure", [{"failed": True}, {"partial": True}, {"interrupted": True}, {"error": "error"}, {"completed": False}])
def test_reaction_never_hides_incomplete_or_failed_turn(failure):
    from gateway.response_filters import reaction_answered
    from gateway.run import _normalize_empty_agent_response
    result = {"api_calls": 1, "acknowledged_by_reaction": True, **failure}
    assert not reaction_answered(result)
    # Keep the older finalization's exact fallback, including interrupted-turn silence.
    baseline = {**result, "acknowledged_by_reaction": False}
    assert _normalize_empty_agent_response(result, "") == _normalize_empty_agent_response(baseline, "")


@pytest.mark.asyncio
async def test_receipts_cross_workers_but_not_executions_or_workspace():
    import asyncio
    from gateway.session import SessionSource
    from gateway.config import Platform
    from gateway.session_context import (
        acknowledge_current_message_reaction, clear_session_vars,
        current_message_reaction_acknowledged, reaction_acknowledgement, set_session_vars, scoped_turn_message,
    )
    async def run(workspace):
        tokens = set_session_vars(platform="slack", chat_id="chat", message_id="message", scope_id=workspace)
        try:
            with reaction_acknowledgement():
                await asyncio.to_thread(acknowledge_current_message_reaction)
                assert current_message_reaction_acknowledged()
                other = SessionSource(platform=Platform.SLACK, chat_id="chat", scope_id="other")
                with scoped_turn_message(other, "message"):
                    assert not current_message_reaction_acknowledged()
                assert current_message_reaction_acknowledged()
            with reaction_acknowledgement():
                assert not current_message_reaction_acknowledged()
        finally:
            clear_session_vars(tokens)
    await asyncio.gather(run("workspace-1"), run("workspace-2"))


@pytest.mark.parametrize("target", [None, "trigger", "reply-anchor", "old-trigger"])
def test_queued_turn_binds_raw_inbound_target_and_restores_outer(monkeypatch, target):
    from types import SimpleNamespace
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from gateway.session_context import (
        acknowledge_current_message_reaction, clear_session_vars,
        current_message_reaction_acknowledged, get_session_env, set_session_vars, scoped_turn_message,
    )
    source = SessionSource(platform=Platform.SLACK, chat_id="new-chat", user_id="new-sender", scope_id="T2", profile="secondary")
    ctx = TurnContext(source=source, message="thanks", history=[], session_id="sid", session_key="key",
                      event_message_id="reply-anchor", inbound_message_id="trigger")
    turn = TurnRunner(SimpleNamespace(), ctx)
    turn._native_image_run_message = lambda: "thanks"

    class Agent:
        def run_conversation(self, *args, **kwargs):
            assert kwargs["persist_user_platform_id"] == "trigger"
            assert get_session_env("HERMES_SESSION_MESSAGE_ID") == "trigger"
            assert get_session_env("HERMES_SESSION_USER_ID") == "new-sender"
            assert get_session_env("HERMES_SESSION_SCOPE_ID") == "T2"
            assert get_session_env("HERMES_SESSION_PROFILE") == "secondary"
            assert not current_message_reaction_acknowledged()
            if target:
                # Deliberately change just the tool target, not the receipt collector.
                with scoped_turn_message(source, target):
                    acknowledge_current_message_reaction()
            return {"final_response": "", "api_calls": 1, "acknowledged_by_reaction": True}

    tokens = set_session_vars(platform="slack", chat_id="old-chat", message_id="old-trigger", user_id="old-sender")
    try:
        result = turn._run_conversation_with_approval(Agent(), [], None, "thanks", None)
        assert result["acknowledged_by_reaction"] is (target == "trigger")
        assert get_session_env("HERMES_SESSION_MESSAGE_ID") == "old-trigger"
        assert get_session_env("HERMES_SESSION_CHAT_ID") == "old-chat"
        assert get_session_env("HERMES_SESSION_USER_ID") == "old-sender"
    finally:
        clear_session_vars(tokens)



@pytest.mark.parametrize("delivered,target", [(True, "trigger"), (False, "trigger"), (True, "older-message")])
@pytest.mark.asyncio
async def test_real_reaction_execution_reaches_gateway_delivery(monkeypatch, tmp_path, delivered, target):
    import asyncio
    import json
    from types import SimpleNamespace
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import send_message_senders, send_message_tool
    from tools.registry import discover_builtin_tools, registry
    discover_builtin_tools()
    runner = _runner(monkeypatch, tmp_path)
    source = _source()
    source.message_id = "reply-anchor"
    turn = TurnRunner(runner, TurnContext(source=source, message="thanks", session_key="key", session_id="sid",
                                          event_message_id="reply-anchor", inbound_message_id="trigger"))
    turn._native_image_run_message = lambda: "thanks"
    loop = asyncio.get_running_loop()
    calls = []
    async def react(chat_id, message_id, emoji, **routing):
        assert asyncio.get_running_loop() is loop
        calls.append((chat_id, message_id))
        return delivered
    live = SimpleNamespace(_gateway_loop=loop)
    monkeypatch.setattr(send_message_senders, "_live_adapter", lambda _: (live, SimpleNamespace(add_reaction=react)))
    monkeypatch.setattr(send_message_tool, "_live_adapter", lambda _: (live, SimpleNamespace(add_reaction=react)))
    monkeypatch.setattr(send_message_tool, "_resolve_tool_target", lambda *_a, **_kw: ("telegram", source.chat_id, None, None))
    monkeypatch.setattr(send_message_tool, "_authorize_relay_target", lambda *_a, **_kw: None)

    class Agent:
        def run_conversation(self, *args, **kwargs):
            if target == "trigger":
                result = json.loads(registry.dispatch("react_to_message", {"emoji": "👍"}))
            else:
                result = json.loads(send_message_tool._handle_react({
                    "target": f"telegram:{source.chat_id}", "message_id": target, "emoji": "👍",
                }))
            assert bool(result.get("success")) is delivered
            return {"final_response": "", "messages": [], "api_calls": 1, "completed": True}

    async def run_agent(*_a, **_kw):
        return await asyncio.to_thread(turn._run_conversation_with_approval, Agent(), [], None, "thanks", None)
    runner._run_agent = run_agent
    tokens = set_session_vars(platform="telegram", chat_id=source.chat_id, message_id="stale-outer-message")
    try:
        response = await runner._handle_message_with_agent(_event(), source, "key", 1)
    finally:
        clear_session_vars(tokens)
    assert calls == [(source.chat_id, target)]
    if delivered and target == "trigger":
        assert not response
    else:
        assert "no response was generated" in response
