"""A delivered reaction answers only its own successful human turn."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.response_filters import reaction_answered
from gateway.run import _normalize_empty_agent_response
from gateway.run_turn_runner import TurnRunner
from gateway.run_turn_silence_recovery import silence_recovery_due
from gateway.session import SessionSource
from gateway.session_context import (
    acknowledge_current_message_reaction, clear_session_vars,
    reaction_acknowledgement, set_session_vars,
)
from gateway.turn_context import TurnContext
from tests.gateway.test_gateway_silence_tokens import _event, _runner, _source


@pytest.mark.parametrize("acknowledged", [True, False])
@pytest.mark.parametrize("text", ["", "NO_REPLY", "[SILENT]", "A substantive reply"])
@pytest.mark.asyncio
async def test_human_delivery_suppresses_only_answered_empty_turn(monkeypatch, tmp_path, text, acknowledged):
    runner = _runner(monkeypatch, tmp_path)
    result = {"final_response": text, "messages": [], "api_calls": 1,
              "completed": True, "acknowledged_by_reaction": acknowledged}
    runner._run_agent = AsyncMock(return_value=result)
    context = TurnContext(source=_source(), message="thanks", history=[], session_id="sid", session_key="key",
                          reply_expected=True, _run_still_current=lambda: True)
    assert silence_recovery_due(runner, result, context, silence_retry=False, scheduled_heartbeat=False) is (not acknowledged and text in ("NO_REPLY", "[SILENT]"))
    turn = _event(reply_expected=True)
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
                assert receipts == {("slack", chat, "message")}
            with reaction_acknowledgement() as receipts:
                assert not receipts
                from gateway.session_context import (
                    current_message_reaction_acknowledged, get_session_env, scoped_turn_message,
                )
                with scoped_turn_message(SimpleNamespace(user_id="api-author"), None):
                    assert get_session_env("HERMES_SESSION_USER_ID") == "api-author"
                    assert get_session_env("HERMES_SESSION_MESSAGE_ID") == ""
                    assert get_session_env("HERMES_SESSION_PLATFORM") == ""
                    acknowledge_current_message_reaction()
                    assert not current_message_reaction_acknowledged()
                assert get_session_env("HERMES_SESSION_MESSAGE_ID") == "message"
                assert get_session_env("HERMES_SESSION_CHAT_ID") == chat
        finally:
            clear_session_vars(tokens)
    await asyncio.gather(run("chat-1"), run("chat-2"))


@pytest.mark.parametrize("target", ["trigger", "older-message"])
@pytest.mark.parametrize("runtime_lock", ["unlocked", "confirmed", "rejected"])
def test_turn_runner_rebinds_queued_message_and_restores_enclosing_turn(monkeypatch, target, runtime_lock):
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
        session_prompt_tokens = 7
        session_completion_tokens = 3

        def run_conversation(self, *args, **kwargs):
            from gateway.session_context import get_session_env
            assert get_session_env("HERMES_SESSION_MESSAGE_ID") == "trigger"
            assert get_session_env("HERMES_SESSION_USER_ID") == "sender"
            acknowledge_current_message_reaction()
            self.session_prompt_tokens += 11
            self.session_completion_tokens += 5
            return {"final_response": "", "api_calls": 1}

    from gateway.session_api_turn import api_execution
    from gateway.session_results import execution_result

    def runtime_metadata(agent, **kwargs):
        assert kwargs["confirmed_runtime_lock"] is True
        if runtime_lock == "rejected":
            raise RuntimeError("served runtime differs from the lock")
        return {"provider": "test", "model": "locked"}

    api_token = api_execution.set({
        "settings": {"confirmed_runtime_lock": runtime_lock != "unlocked"},
        "adapter": SimpleNamespace(_turn_runtime_metadata=runtime_metadata),
    })
    captured = {}
    capture_token = execution_result.set(captured)
    tokens = set_session_vars(platform="slack", chat_id="chat", message_id=target, user_id="older-sender")
    try:
        result = turn._run_conversation_with_approval(Agent(), [], None, "thanks", None)
        from gateway.session_context import get_session_env
        assert result["acknowledged_by_reaction"] is True
        expected_usage = {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16}
        if runtime_lock == "confirmed":
            expected_usage["runtime"] = {"provider": "test", "model": "locked"}
        assert captured["usage"] == expected_usage
        if runtime_lock == "rejected":
            assert result["failed"] is True and result["completed"] is False
            assert result["error"] == "served runtime differs from the lock"
            assert not reaction_answered(result)
        assert get_session_env("HERMES_SESSION_MESSAGE_ID") == target
        assert get_session_env("HERMES_SESSION_USER_ID") == "older-sender"
    finally:
        clear_session_vars(tokens)
        execution_result.reset(capture_token)
        api_execution.reset(api_token)
