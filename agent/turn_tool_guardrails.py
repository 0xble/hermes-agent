"""Bounded guardrail outcomes for one tool-calling round."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCapVerdict:
    """The bounded final-answer result for a loop tool cap."""

    final_response: Any
    interrupted: bool
    turn_exit_reason: str


def handle_tool_cap_answer(agent: Any, messages: Any, api_call_count: Any, decision: Any) -> ToolCapVerdict:
    """Produce the one permitted cap answer without executing returned tools.

    This is the existing cap path split out of the round driver; its state updates and
    display replay intentionally remain in the same order as before.
    """
    turn_exit_reason = "tool_cap_answer"
    interrupted = False
    final_response = ""
    # Every refused call already has a synthetic tool result. Allow one bounded
    # answer from the collected results, but never run tools returned by it.
    try:
        # This completion bypasses the normal request lifecycle. Prior tool-round
        # commentary is not evidence that this final answer reached chat display.
        agent._reset_stream_delivery_tracking()
        final_response = agent._handle_max_iterations(messages, api_call_count, tool_cap=decision)
    except InterruptedError:
        from agent.interrupt_control import interrupted_during_api_call_reason
        interrupted = True
        turn_exit_reason = interrupted_during_api_call_reason(agent)
        final_response = ""
    # A cap completion may already have reached display through its stream.
    # Replay only when chat display did not accept the complete answer.
    if final_response:
        agent._safe_print(f"\n{final_response}\n")
        if agent.stream_delta_callback and not getattr(agent, "_hold_stream", False):
            with suppress(Exception):
                if not agent._interim_content_fully_streamed(final_response):
                    agent.stream_delta_callback(final_response)
                agent.stream_delta_callback(None)
    return ToolCapVerdict(final_response, interrupted, turn_exit_reason)
