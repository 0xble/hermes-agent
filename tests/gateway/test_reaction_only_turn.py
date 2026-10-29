"""A delivered reaction answers only its own successful human turn."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.response_filters import reaction_answered
from gateway.run import _normalize_empty_agent_response
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.session_context import (
    acknowledge_current_message_reaction, clear_session_vars,
    reaction_acknowledgement, set_session_vars,
)
from gateway.turn_context import TurnContext
from tests.gateway.test_gateway_silence_tokens import _event, _runner


@pytest.mark.parametrize("acknowledged", [True, False])
@pytest.mark.parametrize("text", ["", "NO_REPLY", "[SILENT]", "A substantive reply"])
@pytest.mark.asyncio
async def test_human_delivery_suppresses_only_answered_empty_turn(monkeypatch, tmp_path, text, acknowledged):
    runner = _runner(monkeypatch, tmp_path)
    result = {"final_response": text, "messages": [], "api_calls": 1,
              "completed": True, "acknowledged_by_reaction": acknowledged}
    runner._run_agent = AsyncMock(return_value=result)
    turn = _event()
    # Exercise inbound delivery after the turn runner's silence-retry seam.
    response = await runner._handle_message(turn)
    if text == "A substantive reply":
        assert response == text
    elif acknowledged:
        assert not response
        assert runner._run_agent.await_count == 1
    else:
        assert response
        assert response != text


@pytest.mark.parametrize("failure", [{"failed": True}, {"partial": True}, {"interrupted": True}, {"error": "error"}, {"completed": False}])
def test_reaction_does_not_hide_failed_or_incomplete_turn(failure):
    result = {"api_calls": 1, "acknowledged_by_reaction": True, **failure}
    assert not reaction_answered(result)
    if not failure.get("interrupted"):
        assert _normalize_empty_agent_response(result, "")


@pytest.mark.asyncio
async def test_receipts_cross_worker_contexts_but_not_turns_or_sessions():
    async def run(chat):
        tokens = set_session_vars(platform="slack", chat_id=chat, message_id="message")
        try:
            with reaction_acknowledgement() as receipts:
                await asyncio.to_thread(acknowledge_current_message_reaction)
                assert len(receipts) == 1
                from gateway.session_context import current_message_reaction_acknowledged
                assert current_message_reaction_acknowledged()
            with reaction_acknowledgement() as receipts:
                assert not receipts
        finally:
            clear_session_vars(tokens)
    await asyncio.gather(run("chat-1"), run("chat-2"))


@pytest.mark.parametrize("target", ["trigger", "older-message"])
def test_turn_runner_rebinds_queued_message_and_restores_enclosing_turn(monkeypatch, target):
    source = SessionSource(platform=Platform.SLACK, chat_id="chat", user_id="sender")
    ctx = TurnContext(source=source, message="thanks", history=[], session_id="sid", session_key="key",
                      event_message_id="trigger", inbound_message_id="trigger")
    ctx.approval_session_key = "key"
    runner = SimpleNamespace(config=SimpleNamespace(), adapters={})
    turn = TurnRunner(runner, ctx)
    turn._wire_attended_turn_router = lambda *_: None
    turn._native_image_run_message = lambda: "thanks"
    turn._clear_session_approval = lambda *_: None
    turn._tool_progress_callbacks_for_turn = lambda *_: (None, None)

    class Agent:
        def run_conversation(self, *args, **kwargs):
            from gateway.session_context import get_session_env
            assert get_session_env("HERMES_SESSION_MESSAGE_ID") == "trigger"
            assert get_session_env("HERMES_SESSION_USER_ID") == "sender"
            acknowledge_current_message_reaction()
            return {"final_response": "", "api_calls": 1}

    tokens = set_session_vars(platform="slack", chat_id="chat", message_id=target, user_id="older-sender")
    try:
        result = turn._run_conversation_with_approval(Agent(), [], None, "thanks", None)
        from gateway.session_context import get_session_env
        assert result["acknowledged_by_reaction"] is True
        assert get_session_env("HERMES_SESSION_MESSAGE_ID") == target
        assert get_session_env("HERMES_SESSION_USER_ID") == "older-sender"
    finally:
        clear_session_vars(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["NO_REPLY", "[SILENT]"])
@pytest.mark.parametrize("acknowledged", [True, False])
async def test_queued_delivery_uses_its_own_reaction_receipt(text, acknowledged):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    runner = GatewayRunner(GatewayConfig())
    runner._deliver_queued_first_response = AsyncMock()
    ctx = SimpleNamespace(
        session_key="key", stream_consumer_holder=[None], mute_notification_reply=False,
        persist_user_display_kind=None, source=SessionSource(platform=Platform.SLACK, chat_id="chat"),
        _status_thread_metadata=None, event_message_id=None, inbound_message_id="queued", run_generation=1,
    )
    result = {"final_response": text, "completed": True, "api_calls": 1,
              "acknowledged_by_reaction": acknowledged}
    await runner._run_agent_deliver_first_response(ctx, None, result, result, None)
    if acknowledged:
        runner._deliver_queued_first_response.assert_not_awaited()
    else:
        text_sent = runner._deliver_queued_first_response.await_args.args[0]
        assert text_sent and text_sent != text
