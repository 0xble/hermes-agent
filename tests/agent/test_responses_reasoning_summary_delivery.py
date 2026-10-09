"""Responses reasoning summaries are never visible assistant replies."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


SUMMARY = "**Confirming silent final response**"
ENCRYPTED_ITEM = {
    "type": "reasoning",
    "id": "rs_6518674",
    "encrypted_content": "encrypted-reasoning-item",
    "summary": [{"type": "summary_text", "text": SUMMARY}],
}


@pytest.fixture()
def loop_agent():
    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://chatgpt.com/backend-api/codex",
            model="gpt-6.1-sol",
            provider="custom:codex-proxy",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent.api_mode = "chat_completions"
        agent._restore_primary_runtime = lambda: None
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.compression_enabled = False
        agent.save_trajectories = False
        return agent


def _response(content="", *, reasoning=SUMMARY, finish_reason="stop", codex_items=None):
    message = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning=reasoning,
        reasoning_content=None,
        reasoning_details=None,
        codex_reasoning_items=[ENCRYPTED_ITEM] if codex_items is None else codex_items,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model="gpt-6.1-sol",
        usage=None,
    )


def _normalized(agent, response):
    """Responses-normalized shape (what ``_normalize_codex_response`` yields) on a stub client."""
    choice = response.choices[0]
    return SimpleNamespace(**vars(choice.message), finish_reason=choice.finish_reason)


def _run(agent, responses, *, user_message="hello", display_metadata=None):
    agent.client.chat.completions.create.side_effect = list(responses)
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.turn_response_intake.normalize_response_for_agent", _normalized),
    ):
        return agent.run_conversation(
            user_message,
            persist_user_display_metadata=display_metadata,
        )


def test_responses_summary_on_non_reply_turn_becomes_silence(loop_agent):
    result = _run(
        loop_agent,
        [_response()],
        user_message="[relay from=session/child receipt=receipt-1]\nNo reply needed",
        display_metadata={"reply_expected": False},
    )

    assert result["final_response"] == "[SILENT]"
    assert SUMMARY not in result["final_response"]
    assistant = [message for message in result["messages"] if message.get("role") == "assistant"][-1]
    assert assistant.get("content") in (None, "")
    assert assistant.get("api_content") != SUMMARY
    assert loop_agent.client.chat.completions.create.call_count == 1


def test_responses_summary_on_reply_expected_turn_uses_empty_ladder(loop_agent):
    result = _run(
        loop_agent,
        [_response(), _response(content="Visible answer", reasoning=None)],
    )

    assert result["final_response"] == "Visible answer"
    assert SUMMARY not in result["final_response"]
    assert loop_agent.client.chat.completions.create.call_count == 2
    assert all(
        message.get("api_content") != SUMMARY
        for message in result["messages"]
        if message.get("role") == "assistant"
    )


def test_responses_summary_never_leaks_when_the_empty_ladder_is_exhausted(loop_agent):
    result = _run(loop_agent, [_response() for _ in range(12)])

    assert SUMMARY not in (result["final_response"] or "")
    assert loop_agent.client.chat.completions.create.call_count > 1


def test_chat_completions_inline_reasoning_promotion_is_unchanged(loop_agent):
    """vLLM-style parsers file the whole answer as reasoning: no Responses carrier, still promoted."""
    answer = "The answer is 42."
    result = _run(loop_agent, [_response(reasoning=answer, codex_items=[])])

    assert result["final_response"] == answer
    assert loop_agent.client.chat.completions.create.call_count == 1
