"""Regression coverage for partial tool-call stream labels."""

from agent import conversation_loop as cl
from agent.chat_completion_helpers import _build_partial_stream_stub


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


def test_partial_stub_records_streamed_argument_chars():
    stub = _build_partial_stream_stub("assistant", None, None, "model", None,
                                      dropped_tool_names=["terminal"], dropped_tool_argument_chars=128)
    assert stub._dropped_tool_argument_chars == 128
