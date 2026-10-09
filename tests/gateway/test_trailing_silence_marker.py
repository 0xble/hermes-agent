from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import sys

import pytest

from agent.agent_runtime_helpers import strip_think_blocks as _real_strip_think_blocks
from agent.conversation_loop import _DEGENERATE_FINAL_NUDGE, _GATEWAY_SILENCE_REPROMPT_NUDGE
from agent.turn_final_response import finish_text_response
from gateway.run_turn import GatewayTurnMixin


class _ShapeRunner(GatewayTurnMixin):
    def __init__(self):
        self.async_session_store = SimpleNamespace(clear_resume_pending=self._noop)

    async def _noop(self, *_args, **_kwargs):
        return None

    async def _clear_restart_failure_count(self, *_args, **_kwargs):
        return None


def _stub_gateway_run(monkeypatch):
    module = ModuleType("gateway.run")
    module._is_gateway_hidden_reasoning_incomplete_turn = lambda _result: False
    module._normalize_empty_agent_response = lambda _result, response, history_len: response
    module._sanitize_gateway_final_response = lambda _platform, response, interrupted: response
    module._should_clear_resume_pending_after_turn = lambda _result: False
    monkeypatch.setitem(sys.modules, "gateway.run", module)


def _stub_conversation_loop(monkeypatch):
    module = ModuleType("agent.conversation_loop")
    module._CODEX_ACK_CONTINUATION_NUDGE = "continue"
    module._DEGENERATE_FINAL_NUDGE = "continue"
    module._DROPPED_TOOLCALL_NUDGE_CONTENT = "continue"
    setattr(module, "_GATEWAY_SILENCE_REPROMPT_NUDGE", _GATEWAY_SILENCE_REPROMPT_NUDGE)
    module._join_truncated_parts = lambda parts: "".join(text for text, _ in parts)
    monkeypatch.setitem(sys.modules, "agent.conversation_loop", module)


def _stub_runtime_helpers(monkeypatch):
    module = ModuleType("agent.agent_runtime_helpers")
    module.intent_ack_continuation_mode = lambda _agent: "off"
    module.looks_like_degenerate_final = lambda _text, user_message: False
    module.promoted_reasoning_announces_action = lambda _text: False
    module.tool_results_this_turn = lambda _messages: 0
    module.trailing_continue_intent = lambda _text: False
    monkeypatch.setitem(sys.modules, "agent.agent_runtime_helpers", module)


@pytest.mark.asyncio
async def test_non_streaming_gateway_delivery_strips_loop_complete_but_detection_sees_raw(monkeypatch):
    _stub_gateway_run(monkeypatch)
    runner = _ShapeRunner()
    source = SimpleNamespace(chat_id="chat-1", platform=SimpleNamespace(value="telegram"))
    response, intentional, _ = await runner._hmwa_shape_agent_response(
        {"final_response": "Done.\nLOOP_COMPLETE", "messages": [], "api_calls": 1},
        source, history=[], session_entry=SimpleNamespace(session_id="session-1"), session_key=None,
        _quick_key=None, run_generation=0, _run_start_session_id="session-1",
        _platform_name="telegram", _msg_start_time=0.0,
    )

    assert response == "Done.\nLOOP_COMPLETE"
    assert intentional is False


@pytest.mark.asyncio
async def test_non_streaming_gateway_delivery_strips_trailing_marker(monkeypatch):
    _stub_gateway_run(monkeypatch)
    runner = _ShapeRunner()
    source = SimpleNamespace(chat_id="chat-1", platform=SimpleNamespace(value="telegram"))
    response, intentional, _ = await runner._hmwa_shape_agent_response(
        {"final_response": "Done.\n\nNO_REPLY", "messages": [], "api_calls": 1},
        source, history=[], session_entry=SimpleNamespace(session_id="session-1"), session_key=None,
        _quick_key=None, run_generation=0, _run_start_session_id="session-1",
        _platform_name="telegram", _msg_start_time=0.0,
    )

    assert response == "Done."
    assert intentional is False
    assert "NO_REPLY" not in response


class _PersistenceAgent:
    platform = "telegram"
    valid_tool_names = set()
    quiet_mode = True
    _stall_guards = True
    model = "model"
    provider = "provider"
    api_mode = "chat_completions"
    _current_turn_id = "turn-1"

    def _extract_reasoning(self, _message):
        return ""

    def _has_content_after_think_block(self, text):
        return bool(text.strip())

    def _emit_pending_fallback_notice(self):
        return None

    def _clear_status_buffer(self):
        return None

    def _strip_think_blocks(self, text):
        return text

    def _looks_like_codex_intermediate_ack(self, **_kwargs):
        return False

    def _build_assistant_message(self, message, finish_reason):
        return {"role": "assistant", "content": message.content, "finish_reason": finish_reason}

    def _flush_messages_to_session_db(self, messages, _conversation_history):
        self.persisted_messages = list(messages)


def test_non_streaming_persisted_assistant_row_lacks_trailing_marker(monkeypatch):
    _stub_conversation_loop(monkeypatch)
    _stub_runtime_helpers(monkeypatch)
    agent = _PersistenceAgent()
    messages = []
    assistant_message = SimpleNamespace(
        content="Done.\n\nNO_REPLY", tool_calls=[], reasoning_content=None, reasoning_details=None,
    )

    with patch("agent.turn_finalizer.apply_llm_output_transform", side_effect=lambda _agent, text, **_kwargs: (text, False, None)):
        verdict = finish_text_response(
            agent,
            assistant_message=assistant_message, response="", finish_reason="stop",
            messages=messages, api_messages=[], conversation_history=[], api_call_count=1,
            user_message="Please finish", active_system_prompt="", final_response=None,
            _turn_exit_reason=None, _preflight_compression_blocked=None,
            codex_ack_continuations=0, truncated_response_parts=[], length_continue_retries=0,
            _pending_verification_response=None, _pending_verification_response_previewed=False,
            effective_task_id=None,
        )

    assert verdict.action == "break"
    assert verdict.final_response == "Done."
    assert agent.persisted_messages[-1]["content"] == "Done."
    assert "NO_REPLY" not in agent.persisted_messages[-1]["content"]


def test_bare_marker_is_preserved_for_existing_silence_filter():
    from gateway.response_filters import strip_trailing_silence_marker

    assert strip_trailing_silence_marker("NO_REPLY") == "NO_REPLY"
    assert strip_trailing_silence_marker("[SILENT]") == "[SILENT]"
    assert strip_trailing_silence_marker("no reply") == "no reply"


def test_marker_only_runs_collapse_to_one_marker_not_empty():
    from gateway.response_filters import is_intentional_silence_response, strip_trailing_silence_marker

    for text in ("NO_REPLY\n\nNO_REPLY", "[SILENT]\nNO_REPLY", "\n\nNO_REPLY\n"):
        result = strip_trailing_silence_marker(text)
        assert is_intentional_silence_response(result), (text, result)


def test_consecutive_trailing_marker_lines_are_all_removed():
    from gateway.response_filters import strip_trailing_silence_marker

    assert strip_trailing_silence_marker("Done.\n\nNO_REPLY\n[SILENT]") == "Done."


def _run_finish(monkeypatch, agent, content):
    _stub_conversation_loop(monkeypatch)
    _stub_runtime_helpers(monkeypatch)
    assistant_message = SimpleNamespace(
        content=content, tool_calls=[], reasoning_content=None, reasoning_details=None,
    )
    with patch("agent.turn_finalizer.apply_llm_output_transform", side_effect=lambda _agent, text, **_kwargs: (text, False, None)), \
            patch("agent.turn_final_response.recover_empty_response", side_effect=AssertionError("empty-response recovery")):
        return finish_text_response(
            agent,
            assistant_message=assistant_message, response="", finish_reason="stop",
            messages=[], api_messages=[], conversation_history=[], api_call_count=1,
            user_message="FYI", active_system_prompt="", final_response=None,
            _turn_exit_reason=None, _preflight_compression_blocked=None,
            codex_ack_continuations=0, truncated_response_parts=[], length_continue_retries=0,
            _pending_verification_response=None, _pending_verification_response_previewed=False,
            effective_task_id=None,
        )


class _RealThinkAgent(_PersistenceAgent):
    """Uses the production think-block stripper rather than an identity stub."""

    def _strip_think_blocks(self, text):
        return _real_strip_think_blocks(self, text)

    def _has_content_after_think_block(self, text):
        return bool(text) and bool(self._strip_think_blocks(text).strip())


@pytest.mark.parametrize("content", [
    "<think>Nothing to add here.</think>\nNO_REPLY",
    "<think>FYI only.</think>\n\nNO_REPLY\n\nNO_REPLY",
])
def test_inline_think_then_bare_marker_stays_intentional_silence(monkeypatch, content):
    """Reasoning plus a bare marker is silence, not an empty response to retry."""
    from gateway.response_filters import is_intentional_silence_response

    verdict = _run_finish(monkeypatch, _RealThinkAgent(), content)

    assert verdict.action == "break"
    assert is_intentional_silence_response(verdict.final_response)


def test_inline_think_then_prose_then_marker_strips_only_the_marker(monkeypatch):
    agent = _RealThinkAgent()
    verdict = _run_finish(monkeypatch, agent, "<think>Report it.</think>\nDone.\n\nNO_REPLY")

    assert verdict.action == "break"
    assert verdict.final_response == "Done."
    assert "NO_REPLY" not in agent.persisted_messages[-1]["content"]


def test_unclosed_fence_inside_think_block_does_not_hide_trailing_marker(monkeypatch):
    agent = _RealThinkAgent()
    verdict = _run_finish(monkeypatch, agent, "<think>Draft:\n```\nNO_REPLY</think>\nDone.\nNO_REPLY")

    assert verdict.action == "break"
    assert "NO_REPLY" not in verdict.final_response
    assert verdict.final_response.endswith("Done.")
    assert "NO_REPLY" not in agent.persisted_messages[-1]["content"].split("</think>")[-1]


def test_loop_complete_trailing_marker_is_display_only():
    from gateway.response_filters import strip_trailing_loop_complete_marker

    assert strip_trailing_loop_complete_marker("Done.\nLOOP_COMPLETE") == "Done."
    assert strip_trailing_loop_complete_marker("LOOP_COMPLETE") == ""
    assert strip_trailing_loop_complete_marker("Mention LOOP_COMPLETE in prose") == "Mention LOOP_COMPLETE in prose"
    assert strip_trailing_loop_complete_marker("```text\nLOOP_COMPLETE\n```") == "```text\nLOOP_COMPLETE\n```"
    assert strip_trailing_loop_complete_marker("Done.\nLOOP_COMPLETE.") == "Done."


def test_loop_complete_partial_marker_is_held_only_at_top_level_tail():
    from gateway.response_filters import ends_with_partial_loop_complete_marker

    assert ends_with_partial_loop_complete_marker("Done.\nLOOP_COMP")
    assert ends_with_partial_loop_complete_marker("Done.\nLOOP_COMPLETE")
    assert not ends_with_partial_loop_complete_marker("```\nLOOP_COMP")
    assert not ends_with_partial_loop_complete_marker("Mention LOOP_COMP in prose")

def test_loop_complete_split_holds_the_whole_trailing_marker_run():
    """A repeated marker must be held as one run so none of it flashes while streaming."""
    from gateway.response_filters import split_trailing_loop_complete_marker

    assert split_trailing_loop_complete_marker("Done.\nLOOP_COMPLETE\nLOOP_COMPLETE") == (
        "Done.\n", "LOOP_COMPLETE\nLOOP_COMPLETE",
    )
    assert split_trailing_loop_complete_marker("Done.\nLOOP_COMPLETE\n\nLOOP_COM") == (
        "Done.\n", "LOOP_COMPLETE\n\nLOOP_COM",
    )
    assert split_trailing_loop_complete_marker("Done.\nLOOP_COM") == ("Done.\n", "LOOP_COM")
    # A marker-looking line inside a closed fence is content, not part of the run.
    assert split_trailing_loop_complete_marker("```\nLOOP_COMPLETE\n```\nLOOP_COM") == (
        "```\nLOOP_COMPLETE\n```\n", "LOOP_COM",
    )


def test_loop_complete_split_uses_released_fence_context():
    """A fence opened in an earlier chunk and closed in this one: the trailing marker after
    the closing fence is control text and must be held, not released."""
    from gateway.response_filters import split_trailing_loop_complete_marker

    seen = "Example:\n```text\nLOOP_COMPLETE\n"
    assert split_trailing_loop_complete_marker("```\nLOOP_COMPLETE", context=seen) == (
        "```\n", "LOOP_COMPLETE",
    )
    assert split_trailing_loop_complete_marker("```\nLOOP_COM", context=seen) == ("```\n", "LOOP_COM")
    # Still inside the open fence: nothing to hold.
    assert split_trailing_loop_complete_marker("more\nLOOP_COMPLETE", context=seen) == (
        "more\nLOOP_COMPLETE", "",
    )
    # A held run spanning released context only splits this chunk.
    assert split_trailing_loop_complete_marker("LOOP_COMPLETE", context="Done.\nLOOP_COMPLETE\n") == (
        "", "LOOP_COMPLETE",
    )
