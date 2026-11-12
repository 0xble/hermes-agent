"""Regression coverage for partial tool-call stream labels."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent import conversation_loop as cl
from agent.chat_completion_helpers import _StreamingCall, _build_partial_stream_stub
from agent.chat_completion_helpers_tool_calls import _ToolCallAccumulator
from agent.relay_llm import AnthropicStreamAccumulator
from agent.turn_retry_state import TurnRetryState
from agent.turn_truncation import _Trunc, _continue_text
from hermes_constants import PARTIAL_STREAM_STUB_ID


def test_small_dropped_tool_call_uses_transport_guidance():
    prompt = cl._get_continuation_prompt(True, ["terminal"], dropped_tool_argument_chars=128).lower()
    assert "transport interruption" in prompt
    assert "re-issue the same tool call unchanged" in prompt
    assert "too large" not in prompt


def test_large_dropped_tool_call_uses_split_guidance():
    prompt = cl._get_continuation_prompt(
        True, ["terminal"],
        dropped_tool_argument_chars=cl._DROPPED_TOOL_ARGUMENTS_LARGE_THRESHOLD_CHARS + 1,
    ).lower()
    assert "too large" in prompt
    assert "break the content into multiple smaller tool calls" in prompt


def test_second_dropped_tool_continuation_uses_split_guidance():
    prompt = cl._get_continuation_prompt(True, ["write_file"], 128, 2).lower()
    assert "break the content into multiple smaller tool calls" in prompt


def test_partial_stub_records_streamed_argument_chars():
    stub = _build_partial_stream_stub("assistant", None, None, "model", None,
                                      dropped_tool_names=["terminal"], dropped_tool_argument_chars=128)
    assert stub._dropped_tool_argument_chars == 128


def _streaming_call():
    agent = MagicMock()
    agent.model = "model"
    agent.provider = "provider"
    agent.base_url = "https://example.com/v1"
    agent.api_mode = "chat_completions"
    agent._current_streamed_assistant_text = "partial"
    agent._warning_presentation_enabled.return_value = False
    return _StreamingCall(agent, {}, None)


def test_partial_stream_stub_reads_chat_accumulator_argument_chars():
    call = _streaming_call()
    accumulator = _ToolCallAccumulator()
    accumulator.feed(SimpleNamespace(
        index=0, id="call_x", function=SimpleNamespace(name="write_file", arguments='{"path":'),
    ))
    call._partial_tool_accumulator = accumulator
    call.result["partial_tool_names"] = ["write_file"]

    stub = call._partial_stream_stub(RuntimeError("stall"))

    assert stub._dropped_tool_argument_chars == len('{"path":')


def test_partial_stream_stub_reads_anthropic_accumulator_argument_chars():
    call = _streaming_call()
    accumulator = AnthropicStreamAccumulator()
    accumulator._blocks[0] = {"_partial_json": '{"path":'}
    call._partial_tool_accumulator = accumulator
    call.result["partial_tool_names"] = ["write_file"]

    stub = call._partial_stream_stub(RuntimeError("stall"))

    assert stub._dropped_tool_argument_chars == len('{"path":')


def test_clean_eof_stub_carries_argument_chars():
    call = _streaming_call()
    tool_calls = {
        0: {"id": "call_x", "type": "function", "function": {
            "name": "write_file", "arguments": '{"path":',
        }},
    }

    stub = call._finish_chat_stream(
        SimpleNamespace(response=None), "assistant", [], [], tool_calls, None,
        "model", None, flush_pending=lambda: None,
    )

    assert stub._clean_eof is True
    assert stub._dropped_tool_argument_chars == len('{"path":')


def test_continue_text_passes_argument_chars_into_nudge():
    response = SimpleNamespace(
        id=PARTIAL_STREAM_STUB_ID,
        _dropped_tool_names=["write_file"],
        _dropped_tool_argument_chars=128,
    )
    agent = MagicMock()
    agent.log_prefix = ""
    agent._session_messages = []
    st = _Trunc(
        agent=agent, response=response, finish_reason="length", conversation_history=None,
        api_call_count=1, effective_task_id=None, current_turn_user_idx=0,
        messages=[], length_continue_retries=1, truncated_response_parts=[],
        truncated_tool_call_retries=0, retry_count=0, compression_attempts=0,
    )
    captured = {}

    def capture(*args):
        captured["args"] = args
        return "nudge"

    with patch("agent.conversation_loop._get_continuation_prompt", side_effect=capture):
        _continue_text(st, TurnRetryState(), SimpleNamespace(content=None))

    assert captured["args"] == (True, ["write_file"], 128, 2)


def test_start_stream_attempt_resets_partial_accumulator():
    call = _streaming_call()
    call._partial_tool_accumulator = object()
    call.result["partial_tool_names"] = ["write_file"]

    call._start_stream_attempt()

    assert call._partial_tool_accumulator is None
    assert call.result["partial_tool_names"] == []
