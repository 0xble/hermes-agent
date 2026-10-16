"""Focused provider-output-cap and preflight-fit regressions for the #613 split."""

from types import SimpleNamespace

from agent.anthropic_adapter import (
    anthropic_validates_output_against_window,
    anthropic_wire_output_cap,
    build_anthropic_kwargs,
)
from agent.chat_completion_helpers import effective_wire_max_output_tokens, selected_max_output_tokens
from agent.preflight_output_fit import affordable_output_cap, request_fits
from agent.transports.chat_completions import resolve_chat_max_tokens


def _agent(**over):
    values = dict(
        api_mode="chat_completions",
        provider="openrouter",
        model="test/model",
        max_tokens=4096,
        _ephemeral_max_output_tokens=None,
        _preflight_output_ceiling=None,
        _ephemeral_reasoning_off=False,
        reasoning_config=None,
        tools=None,
        context_compressor=SimpleNamespace(context_length=16_384),
    )
    values.update(over)
    return SimpleNamespace(**values)


def test_chat_ceiling_is_reapplied_to_retry_and_gemini_floor_wins():
    reasoning = {"enabled": True, "effort": "medium"}
    assert resolve_chat_max_tokens("test/model", reasoning, None, 12_000, output_ceiling=5_000) == 5_000
    assert resolve_chat_max_tokens("gemini-3-pro-preview", reasoning, None, 12_000, output_ceiling=5_000) == 65_535


def test_selected_cap_peek_does_not_consume_retry_override():
    agent = _agent(_ephemeral_max_output_tokens=2_000, _preflight_output_ceiling=1_500)
    assert selected_max_output_tokens(agent) == 1_500
    assert selected_max_output_tokens(agent) == 1_500
    assert agent._ephemeral_max_output_tokens == 2_000


def test_anthropic_wire_cap_preserves_manual_thinking_floor():
    reasoning = {"enabled": True, "effort": "medium"}
    kwargs = build_anthropic_kwargs(
        "claude-3-7-sonnet-20250219", [{"role": "user", "content": "hi"}], None, None,
        reasoning, context_length=64_000, output_ceiling=5_000,
    )
    assert kwargs["max_tokens"] == 12_096
    assert anthropic_wire_output_cap(
        "claude-3-7-sonnet-20250219", None, reasoning, context_length=64_000,
        output_ceiling=5_000,
    ) == kwargs["max_tokens"]
    assert anthropic_validates_output_against_window("claude-3-7-sonnet-20250219")
    assert not anthropic_validates_output_against_window("claude-sonnet-4-5")


def test_anthropic_transport_reapplies_turn_ceiling_on_retries():
    from agent.chat_completion_helpers import _build_anthropic_kwargs
    from agent.transports.anthropic import AnthropicTransport

    agent = SimpleNamespace(
        model="claude-3-7-sonnet-20250219", max_tokens=None,
        _ephemeral_max_output_tokens=None, _preflight_output_ceiling=5_000,
        _ephemeral_output_cap_is_retry_boost=False, _is_anthropic_oauth=False,
        context_compressor=SimpleNamespace(context_length=200_000),
        _get_transport=lambda: AnthropicTransport(),
        _prepare_anthropic_messages_for_api=lambda messages: messages,
        _anthropic_preserve_dots=lambda: False,
        _anthropic_base_url=None,
    )

    first = _build_anthropic_kwargs(agent, [{"role": "user", "content": "one"}], [], None, {})
    retry = _build_anthropic_kwargs(agent, [{"role": "user", "content": "two"}], [], None, {})
    agent._ephemeral_max_output_tokens = 12_000
    boosted_retry = _build_anthropic_kwargs(agent, [{"role": "user", "content": "three"}], [], None, {})

    assert [first["max_tokens"], retry["max_tokens"], boosted_retry["max_tokens"]] == [5_000] * 3


def test_request_fit_prices_tools_and_wire_native_checkpoint():
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    agent = _agent(
        api_mode="codex_responses", provider="openai-codex", model="gpt-5.6",
        base_url="https://chatgpt.com/backend-api/codex", _base_url_hostname="chatgpt.com",
        _base_url_lower="https://chatgpt.com/backend-api/codex", codex_responses_native_compaction=True,
        compression_enabled=True, _codex_reasoning_replay_enabled=True, tools=tools,
        context_compressor=SimpleNamespace(context_length=8_192), max_tokens=2_000,
    )
    messages = [{"role": "user", "content": "follow-up"}]
    assert request_fits(agent, messages, "", wire=True, tools=tools)
    assert affordable_output_cap(agent, messages, tools=tools) is not None


def test_bedrock_cap_is_not_rewritten_by_preflight_ceiling():
    agent = _agent(api_mode="bedrock_converse", provider="bedrock", max_tokens=8_192,
                   _preflight_output_ceiling=1_000)
    assert effective_wire_max_output_tokens(agent) == (8_192, True)


def test_affordable_cap_is_turn_scoped_and_fails_when_no_useful_runway():
    agent = _agent(max_tokens=12_000, context_compressor=SimpleNamespace(context_length=2_000))
    messages = [{"role": "user", "content": "large " * 3_000}]
    assert affordable_output_cap(agent, messages) is None
    agent._preflight_output_ceiling = 1_000
    assert selected_max_output_tokens(agent) == 1_000
    agent._preflight_output_ceiling = None
    assert selected_max_output_tokens(agent) == 12_000
