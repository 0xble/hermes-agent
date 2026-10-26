"""Confirmed reactions bypass only clean empty-response recovery, not truncation."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import turn_final_response as final
from gateway.session_context import (
    acknowledge_current_message_reaction, clear_session_vars,
    reaction_acknowledgement, set_session_vars,
)


@pytest.mark.parametrize("delivered", [True, False])
@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_empty_final_after_reaction_is_a_clean_stop(monkeypatch, delivered, finish_reason):
    agent = SimpleNamespace(
        platform="slack", model="test", provider="test", api_mode="chat_completions",
        valid_tool_names={"react"}, quiet_mode=True,
        _strip_think_blocks=lambda text: text or "",
        _has_content_after_think_block=lambda text: bool(text),
        _build_assistant_message=lambda message, reason: {"role": "assistant", "content": message.content},
        _emit_pending_fallback_notice=MagicMock(), _clear_status_buffer=MagicMock(),
        _buffer_diagnostic_status=MagicMock(), _emit_diagnostic_status=MagicMock(),
        _flush_messages_to_session_db=MagicMock(), _stall_guards=True,
    )
    monkeypatch.setattr(final, "answer_in_reasoning_capability", lambda _: False)
    monkeypatch.setattr("agent.agent_runtime_helpers.intent_ack_continuation_mode", lambda _: "off")
    monkeypatch.setattr(final, "apply_stop_gates", lambda *a, **kw: SimpleNamespace(
        continue_turn=False, pending_verification_response=None, pending_verification_response_previewed=False))
    monkeypatch.setattr("agent.turn_finalizer.apply_llm_output_transform", lambda a, text, **kw: (text, False, None))
    messages = [{"role": "user", "content": "thanks"}, {"role": "tool", "content": "{}", "tool_call_id": "reaction"}]
    tokens = set_session_vars(platform="slack", chat_id="chat", message_id="trigger")
    try:
        with reaction_acknowledgement():
            if delivered:
                acknowledge_current_message_reaction()
            verdict = final.finish_text_response(
                agent, assistant_message=SimpleNamespace(content="", tool_calls=[]),
                response=SimpleNamespace(), finish_reason=finish_reason,
                messages=messages, api_messages=[], conversation_history=[], api_call_count=2,
                user_message="thanks", active_system_prompt="system", final_response=None,
                _turn_exit_reason=None, _preflight_compression_blocked=False,
                codex_ack_continuations=0, truncated_response_parts=[], length_continue_retries=0,
                _pending_verification_response=None, _pending_verification_response_previewed=False,
                effective_task_id="task",
            )
        if delivered and finish_reason == "stop":
            assert verdict.action == "break"
            assert verdict.final_response == ""
            agent._buffer_diagnostic_status.assert_not_called()
            agent._emit_diagnostic_status.assert_not_called()
            assert not any(message.get("_empty_recovery_synthetic") for message in messages)
        else:
            assert verdict.action == "continue"
            agent._buffer_diagnostic_status.assert_called_once()
            assert messages[-1]["_empty_recovery_synthetic"]
    finally:
        clear_session_vars(tokens)
