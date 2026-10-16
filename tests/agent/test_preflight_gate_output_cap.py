"""run_preflight_gate branches added by the provider output-cap preflight."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import preflight_output_fit as fit
from agent import turn_preflight
from agent.turn_preflight_gate import run_preflight_gate

LOCK_HOLDER = "pid=1:tid=2:agent=aa:nonce=bb"
_BIG = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}, {"role": "user", "content": "z"}]


def _compressor(**over):
    values = dict(
        context_length=10_000, threshold_tokens=5_000, protect_first_n=0, protect_last_n=0,
        should_compress=lambda _t=None: False,
        should_defer_preflight_to_real_usage=lambda _t: False,
        get_active_compression_failure_cooldown=lambda: None,
    )
    values.update(over)
    return SimpleNamespace(**values)


def _agent(**over):
    a = SimpleNamespace(
        api_mode="chat_completions", provider="openrouter", model="test/model", tools=None,
        _ollama_num_ctx=None, compression_enabled=True, context_compressor=_compressor(),
        _preflight_output_ceiling=None, _ephemeral_max_output_tokens=None,
        _request_pressure_anchored=True, session_id="s1", log_prefix="", _api_call_count=1,
        _compression_skipped_due_to_lock=None, _compression_blocked_transient=None,
        _last_compression_timed_out=False,
        iteration_budget=SimpleNamespace(refund=MagicMock()),
        _persist_session=MagicMock(), _flush_status_buffer=MagicMock(), _emit_status=MagicMock(),
        _emit_diagnostic_status=MagicMock(), _vprint=MagicMock(), _buffer_status=MagicMock(),
    )
    for k, v in over.items():
        setattr(a, k, v)
    return a


def _gate(agent, *, messages=None, pressure=9_000, attempts=0, max_attempts=3):
    messages = list(_BIG) if messages is None else messages
    return run_preflight_gate(
        agent, request_pressure_tokens=pressure, _moa_prepared_request=None,
        pending_moa_prepared_request=None, messages=messages, system_message="sys",
        user_message="z", active_system_prompt="sys", conversation_history=list(messages),
        api_call_count=1, compression_attempts=attempts, max_compression_attempts=max_attempts,
        effective_task_id="t", final_response=None, failed=False, _turn_exit_reason=None,
        _compression_timeout_exhausted=False, _preflight_compression_blocked=False,
        _provider_overflow_recovery_pending=False, _last_preflight_pressure=None,
        api_messages=messages, tools_for_api=None,
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Keep the guarded compression chain hermetic: no feasibility probe, no local
    # window growth, no review-fork latch, no reference handoff.
    monkeypatch.setattr(turn_preflight, "ensure_compression_feasibility_checked", lambda *_a, **_k: None)
    monkeypatch.setattr(turn_preflight, "_review_fork_first_request_pending", lambda _a: False)
    monkeypatch.setattr(turn_preflight, "_reset_retry_state_after_compaction", lambda _a: None)
    monkeypatch.setattr(
        turn_preflight, "conversation_history_after_compression", lambda _a, m, _h=None: list(m)
    )
    monkeypatch.setattr(turn_preflight, "automatic_compaction_status_message", lambda *_a, **_k: None)
    import agent.conversation_loop as loop
    monkeypatch.setattr(loop, "_maybe_grow_local_window", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "_should_skip_model_call_for_reference_handoff", lambda *_a: False)


def _fits_when(monkeypatch, predicate, *, affordable=None, wire_tokens=9_000):
    import agent.turn_preflight_gate as gate
    monkeypatch.setattr(fit, "request_fits", lambda agent, msgs, *_a, **_k: predicate(agent, msgs))
    monkeypatch.setattr(fit, "affordable_output_cap", lambda *_a, **_k: affordable)
    monkeypatch.setattr(fit, "wire_accurate_input_tokens", lambda *_a, **_k: wire_tokens)
    monkeypatch.setattr(gate, "_arm_output_ceiling", _arm)


def _arm(agent, shrunk):
    agent._preflight_output_ceiling = shrunk
    agent._ephemeral_max_output_tokens = shrunk
    return True


def test_ceiling_arms_and_request_continues(monkeypatch):
    agent = _agent()
    _fits_when(monkeypatch, lambda a, _m: a._preflight_output_ceiling is not None, affordable=5_000)
    v = _gate(agent)
    assert v.action == "fallthrough"
    assert agent._preflight_output_ceiling == 5_000
    assert not v.failed


def test_recovery_compacts_then_continues(monkeypatch):
    agent = _agent(_request_pressure_anchored=False)
    small = [{"role": "user", "content": "z"}]
    agent._compress_context = MagicMock(return_value=(small, "sys2"))
    _fits_when(monkeypatch, lambda _a, m: len(m) == 1)
    v = _gate(agent)
    assert v.action == "continue"
    assert v.messages == small
    assert v.compression_attempts == 1
    assert agent._preflight_output_ceiling is None
    assert agent._ephemeral_max_output_tokens is None
    agent._compress_context.assert_called_once()


def test_hard_window_fail_closed_retains_session(monkeypatch):
    agent = _agent(compression_enabled=False, _preflight_output_ceiling=2_000,
                   _request_pressure_anchored=False)
    _fits_when(monkeypatch, lambda *_: False)
    v = _gate(agent)
    assert v.action == "return"
    assert v._compression_timeout_exhausted is False
    result = v.result
    assert "compression_exhausted" not in result
    assert result["failure_reason"] == "input_retained"
    assert result["failure_retryable"] is True
    assert result["turn_exit_reason"] == "input_retained"
    assert "No provider call was sent" in result["final_response"]
    agent._persist_session.assert_called_once()
    assert agent._preflight_output_ceiling is None


def test_no_progress_compaction_fails_closed_as_retained(monkeypatch):
    agent = _agent(_request_pressure_anchored=False)
    agent._compress_context = MagicMock(side_effect=lambda m, *_a, **_k: (m, "sys"))
    _fits_when(monkeypatch, lambda *_: False)
    v = _gate(agent, attempts=2, max_attempts=3)
    if v.action == "continue":  # first no-progress pass re-enters; next pass is at the cap
        v = _gate(agent, messages=v.messages, attempts=v.compression_attempts, max_attempts=3)
    assert v.action == "return"
    assert "compression_exhausted" not in v.result
    assert v.result["failure_reason"] == "input_retained"


def test_lock_deferral_returns_compression_deferred(monkeypatch):
    agent = _agent(_request_pressure_anchored=False)

    def _lock_skip(messages, *_a, **_k):
        agent._compression_skipped_due_to_lock = LOCK_HOLDER
        return messages, "sys"

    agent._compress_context = _lock_skip
    _fits_when(monkeypatch, lambda *_: False)
    v = _gate(agent)
    assert v.action == "return"
    assert v.result["compression_deferred"] is True
    assert v.result["failed"] is False
    assert "compression_exhausted" not in v.result
    assert v.compression_attempts == 0
    assert agent._preflight_output_ceiling is None


def test_cooldown_returns_compression_deferred(monkeypatch):
    agent = _agent(
        _request_pressure_anchored=False,
        context_compressor=_compressor(
            get_active_compression_failure_cooldown=lambda: 30.0,
        ),
    )
    agent._compress_context = MagicMock()
    _fits_when(monkeypatch, lambda *_: False)
    v = _gate(agent)
    assert v.action == "return"
    assert v.result["compression_deferred"] is True
    assert "compression_exhausted" not in v.result
    agent._compress_context.assert_not_called()


def test_rough_estimate_never_fails_closed(monkeypatch):
    """Without an anchor or native wire estimate the hard-window exit cannot fire."""
    agent = _agent()
    agent._compress_context = MagicMock()
    _fits_when(monkeypatch, lambda *_: False, wire_tokens=None)
    v = _gate(agent)
    assert v.action == "fallthrough"
    agent._compress_context.assert_not_called()


def test_wire_accurate_tokens_require_anchor_or_native(monkeypatch):
    import agent.codex_responses_adapter as adapter
    monkeypatch.setattr(adapter, "estimate_native_responses_preflight_tokens", lambda *_a, **_k: None)
    rough = SimpleNamespace(_request_pressure_anchored=False)
    assert fit.wire_accurate_input_tokens(rough, _BIG, request_pressure_tokens=9_000) is None
    anchored = SimpleNamespace(_request_pressure_anchored=True)
    assert fit.wire_accurate_input_tokens(anchored, _BIG, request_pressure_tokens=9_000) == 9_000
    monkeypatch.setattr(adapter, "estimate_native_responses_preflight_tokens", lambda *_a, **_k: 7_000)
    assert fit.wire_accurate_input_tokens(rough, _BIG, request_pressure_tokens=9_000) == 7_000


@pytest.mark.parametrize(
    "provider,base_url,expected",
    [
        ("openai-codex", "https://chatgpt.com/backend-api/codex", (None, True)),
        ("xai", "https://api.x.ai/v1", (32_000, True)),
        ("github-copilot", "https://models.github.ai/inference", (32_000, True)),
    ],
)
def test_codex_responses_prices_only_caps_sent_by_transport(provider, base_url, expected):
    from agent.chat_completion_helpers import effective_wire_max_output_tokens

    agent = SimpleNamespace(api_mode="codex_responses", max_tokens=32_000,
                            _ephemeral_max_output_tokens=None, _preflight_output_ceiling=None,
                            provider=provider, base_url=base_url)
    assert effective_wire_max_output_tokens(agent) == expected


def test_checkpoint_latch_prefers_native_wire_over_stale_anchor(monkeypatch):
    import agent.codex_responses_adapter as adapter

    monkeypatch.setattr(adapter, "estimate_native_responses_preflight_tokens", lambda *_a, **_k: 7_000)
    agent = SimpleNamespace(
        _request_pressure_anchored=True,
        context_compressor=SimpleNamespace(awaiting_real_usage_after_compression=True),
    )
    assert fit.wire_accurate_input_tokens(agent, _BIG, request_pressure_tokens=180_000) == 7_000


def test_checkpoint_latch_does_not_fail_closed_on_stale_anchor(monkeypatch):
    agent = _agent(
        api_mode="codex_responses",
        compression_enabled=False,
        context_compressor=_compressor(awaiting_real_usage_after_compression=True),
    )
    agent._compress_context = MagicMock()
    _fits_when(monkeypatch, lambda *_: False, wire_tokens=180_000)
    v = _gate(agent, pressure=180_000)
    assert v.action == "return"
    assert v.result["failure_reason"] == "input_retained"
    agent._compress_context.assert_not_called()


def test_anchored_overflow_fails_closed_without_mocking_wire_count(monkeypatch):
    """Review repro: compression disabled, anchor 190k, window 200k, max_tokens 16k.

    The native estimator returns nothing for chat/Anthropic, so the only wire figure is
    the usage anchor. That proven overflow must retain the input instead of falling
    through with no ceiling. Does not mock ``wire_accurate_input_tokens``.
    """
    import agent.codex_responses_adapter as adapter

    monkeypatch.setattr(adapter, "estimate_native_responses_preflight_tokens", lambda *_a, **_k: None)
    agent = _agent(
        max_tokens=16_000,
        reasoning_config=None,
        compression_enabled=False,
        context_compressor=_compressor(context_length=200_000, threshold_tokens=100_000),
    )
    agent._compress_context = MagicMock()
    messages = [{"role": "user", "content": "short"}]
    assert fit.wire_accurate_input_tokens(agent, messages, request_pressure_tokens=190_000) == 190_000
    assert fit.affordable_output_cap(agent, messages, input_tokens=190_000) is None
    assert not fit.request_fits(agent, messages, "", wire=True, input_tokens=190_000)
    v = _gate(agent, messages=messages, pressure=190_000)
    assert v.action == "return"
    result = v.result
    assert result is not None
    assert result["failure_reason"] == "input_retained"
    assert result["turn_exit_reason"] == "input_retained"
    assert "compression_exhausted" not in result
    assert agent._preflight_output_ceiling is None
    agent._compress_context.assert_not_called()


@pytest.mark.parametrize(
    ("context_length", "input_tokens", "max_tokens"),
    [(8_000, 5_000, 4_096), (16_000, 13_000, 8_192), (200_000, 188_000, 8_192)],
)
def test_anchored_fit_rejection_uses_framing_reserve_not_raw_five_percent(
    monkeypatch, context_length, input_tokens, max_tokens
):
    """Any remaining provider-grounded fit rejection retains the input.

    The first two cases leave less than the useful 4096-token runway. The large-window
    case is the same class with an 8192-token cap and is the regression for a request
    that is above the window only after framing reserve and output are priced together.
    """
    import agent.codex_responses_adapter as adapter

    monkeypatch.setattr(adapter, "estimate_native_responses_preflight_tokens", lambda *_a, **_k: None)
    agent = _agent(
        max_tokens=max_tokens,
        reasoning_config=None,
        compression_enabled=False,
        context_compressor=_compressor(
            context_length=context_length,
            threshold_tokens=max(1, context_length // 2),
        ),
    )
    agent._compress_context = MagicMock()
    messages = [{"role": "user", "content": "short"}]
    assert fit.wire_accurate_input_tokens(
        agent, messages, request_pressure_tokens=input_tokens
    ) == input_tokens
    assert not fit.request_fits(agent, messages, "", wire=True, input_tokens=input_tokens)
    v = _gate(agent, messages=messages, pressure=input_tokens)
    assert v.action == "return"
    assert v.result["failure_reason"] == "input_retained"
    agent._compress_context.assert_not_called()


def test_anchored_small_window_below_threshold_still_fails_closed_when_attempt_capped(
    monkeypatch,
):
    """Hard fit evidence wins even when automatic compression would stay below threshold."""
    import agent.codex_responses_adapter as adapter

    monkeypatch.setattr(adapter, "estimate_native_responses_preflight_tokens", lambda *_a, **_k: None)
    agent = _agent(
        max_tokens=4_096,
        reasoning_config=None,
        compression_enabled=True,
        context_compressor=_compressor(
            context_length=8_000,
            threshold_tokens=7_000,
            should_compress=lambda _tokens: False,
        ),
    )
    agent._compress_context = MagicMock()
    messages = [{"role": "user", "content": "short"}]
    v = _gate(agent, messages=messages, pressure=5_000, attempts=3, max_attempts=3)

    assert v.action == "return"
    assert v.result is not None
    assert v.result["failure_reason"] == "input_retained"
    agent._compress_context.assert_not_called()


@pytest.mark.parametrize("api_mode", ["chat_completions", "anthropic_messages"])
def test_anchored_overflow_fails_closed_when_compression_exhausted(monkeypatch, api_mode):
    """An attempt-capped anchored overflow still exits retained, not fallthrough."""
    import agent.codex_responses_adapter as adapter

    monkeypatch.setattr(adapter, "estimate_native_responses_preflight_tokens", lambda *_a, **_k: None)
    agent = _agent(
        api_mode=api_mode,
        model="claude-sonnet-4-5",
        provider="anthropic",
        max_tokens=16_000,
        reasoning_config=None,
        compression_enabled=True,
        _anthropic_preserve_dots=lambda: False,
        context_compressor=_compressor(context_length=200_000, threshold_tokens=100_000),
    )
    agent._compress_context = MagicMock()
    messages = [{"role": "user", "content": "short"}]
    v = _gate(agent, messages=messages, pressure=190_000, attempts=3, max_attempts=3)
    assert v.action == "return"
    result = v.result
    assert result is not None
    assert result["failure_reason"] == "input_retained"
    agent._compress_context.assert_not_called()


def test_unstubbed_mock_compressor_does_not_defer_anchored_wire(monkeypatch):
    """An unstubbed MagicMock latch is not a real bool and must not defer fail-closed."""
    import agent.codex_responses_adapter as adapter

    monkeypatch.setattr(adapter, "estimate_native_responses_preflight_tokens", lambda *_a, **_k: None)
    compressor = MagicMock()
    compressor.context_length = 200_000
    compressor.threshold_tokens = 100_000
    compressor.protect_first_n = 0
    compressor.protect_last_n = 0
    compressor.should_compress.return_value = False
    compressor.get_active_compression_failure_cooldown.return_value = None
    agent = _agent(
        max_tokens=16_000,
        reasoning_config=None,
        compression_enabled=False,
        context_compressor=compressor,
    )
    messages = [{"role": "user", "content": "short"}]
    assert fit.wire_accurate_input_tokens(agent, messages, request_pressure_tokens=190_000) == 190_000
    v = _gate(agent, messages=messages, pressure=190_000)
    assert v.action == "return"
    result = v.result
    assert result is not None
    assert result["failure_reason"] == "input_retained"


def test_real_checkpoint_latch_still_prefers_native_over_anchor(monkeypatch):
    """A real armed bool still withholds a stale usage anchor when a native figure exists."""
    import agent.codex_responses_adapter as adapter

    monkeypatch.setattr(adapter, "estimate_native_responses_preflight_tokens", lambda *_a, **_k: 7_000)
    compressor = MagicMock()
    compressor.awaiting_real_usage_after_compression = True
    agent = SimpleNamespace(
        _request_pressure_anchored=True,
        context_compressor=compressor,
    )
    assert fit.wire_accurate_input_tokens(agent, _BIG, request_pressure_tokens=180_000) == 7_000


def test_native_checkpoint_overflow_fails_closed_with_real_wire_estimate():
    """An armed checkpoint latch does not permit a rejected pruned payload to fall through.

    The native estimator runs against the actual checkpoint conversion and a large retained
    tail; only the stale usage anchor is omitted. This exercises the production wire figure
    rather than replacing it with a mocked ``wire_accurate_input_tokens`` result.
    """
    agent = _agent(
        api_mode="codex_responses",
        provider="openai-codex",
        model="gpt-5.6",
        base_url="https://chatgpt.com/backend-api/codex",
        _base_url_hostname="chatgpt.com",
        _base_url_lower="https://chatgpt.com/backend-api/codex",
        codex_responses_native_compaction=True,
        _codex_reasoning_replay_enabled=True,
        compression_enabled=True,
        _request_pressure_anchored=False,
        context_compressor=_compressor(
            context_length=200_000,
            threshold_tokens=100_000,
            awaiting_real_usage_after_compression=True,
        ),
    )
    agent._compress_context = MagicMock()
    messages = [
        {
            "role": "assistant",
            "content": "checkpointed",
            "codex_reasoning_items": [
                {
                    "type": "compaction",
                    "encrypted_content": "blob",
                    "_issuer_kind": "codex_backend",
                }
            ],
        },
        {"role": "user", "content": "x" * 760_000},
    ]
    native_tokens = fit.wire_accurate_input_tokens(
        agent, messages, request_pressure_tokens=190_000
    )
    assert native_tokens is not None
    assert 189_000 <= native_tokens <= 191_000
    assert not fit.request_fits(agent, messages, "", wire=True, input_tokens=native_tokens)

    v = _gate(agent, messages=messages, pressure=190_000, attempts=3, max_attempts=3)

    assert v.action == "return"
    assert v.result is not None
    assert v.result["failure_reason"] == "input_retained"
    assert v.result["turn_exit_reason"] == "input_retained"
    agent._compress_context.assert_not_called()


def test_provider_thinking_floor_blocks_unsafe_shrink(monkeypatch):
    """A preflight cap never undercuts Anthropic's manual-thinking floor."""
    agent = _agent(
        api_mode="anthropic_messages",
        provider="anthropic",
        model="claude-3-7-sonnet-20250219",
        max_tokens=4_096,
        reasoning_config={"enabled": True, "effort": "medium"},
        _anthropic_preserve_dots=lambda: False,
        compression_enabled=False,
        context_compressor=_compressor(context_length=16_000, threshold_tokens=8_000),
    )
    agent._compress_context = MagicMock()
    messages = [{"role": "user", "content": "short"}]
    input_tokens = 4_000
    assert not fit.request_fits(agent, messages, "", wire=True, input_tokens=input_tokens)
    v = _gate(agent, messages=messages, pressure=input_tokens)

    assert v.action == "return"
    assert v.result is not None
    assert v.result["failure_reason"] == "input_retained"
    assert agent._preflight_output_ceiling is None
    assert agent._ephemeral_max_output_tokens is None
    agent._compress_context.assert_not_called()


def test_anchored_shrink_uses_wire_tokens_not_rough_estimate(monkeypatch):
    """Review repro: anchored 185k in a 200k window must not arm a 189k cap from chars/4.

    Does not mock ``affordable_output_cap``. The rough estimator sees a short message and
    would leave room for ~190k output; the anchored figure is what the gate decided on.
    """
    import agent.codex_responses_adapter as adapter

    monkeypatch.setattr(adapter, "estimate_native_responses_preflight_tokens", lambda *_a, **_k: None)
    agent = _agent(
        max_tokens=32_000,
        _ephemeral_max_output_tokens=8_000,
        reasoning_config=None,
        context_compressor=_compressor(context_length=200_000, threshold_tokens=100_000),
    )
    messages = [{"role": "user", "content": "short"}]
    shrunk = fit.affordable_output_cap(agent, messages, input_tokens=185_000)
    assert shrunk == 4_999
    assert shrunk < 32_000
    v = _gate(agent, messages=messages, pressure=185_000)
    assert v.action == "fallthrough"
    assert agent._preflight_output_ceiling == 4_999
    assert agent._ephemeral_max_output_tokens == 4_999
    assert fit.request_fits(agent, messages, "", wire=True, input_tokens=185_000)


def test_anchored_shrink_keeps_smaller_provider_ephemeral(monkeypatch):
    """A provider-reported overflow floor is not replaced by a larger local room."""
    import agent.codex_responses_adapter as adapter

    monkeypatch.setattr(adapter, "estimate_native_responses_preflight_tokens", lambda *_a, **_k: None)
    agent = _agent(
        max_tokens=32_000,
        _ephemeral_max_output_tokens=4_500,
        reasoning_config=None,
        context_compressor=_compressor(context_length=200_000, threshold_tokens=100_000),
    )
    messages = [{"role": "user", "content": "short"}]
    assert fit.affordable_output_cap(agent, messages, input_tokens=185_000) == 4_500
    v = _gate(agent, messages=messages, pressure=185_000)
    assert v.action == "fallthrough"
    assert agent._ephemeral_max_output_tokens == 4_500


def test_rough_pressure_does_not_fail_closed_without_wire_measurement(monkeypatch):
    """A chars/4 pressure figure can trigger ordinary compression, not a hard abort."""
    agent = _agent(
        _request_pressure_anchored=False,
        context_compressor=_compressor(should_compress=lambda tokens: tokens >= 100),
    )
    agent._compress_context = MagicMock(return_value=([{"role": "user", "content": "z"}], "sys"))
    monkeypatch.setattr(fit, "wire_accurate_input_tokens", lambda *_a, **_k: None)
    monkeypatch.setattr(fit, "request_fits", lambda *_a, **_k: True)
    v = _gate(agent, pressure=200)
    assert v.action == "continue"
    assert v.result is None
    agent._compress_context.assert_called_once()


def test_length_retry_boost_is_not_reclamped_by_stale_ceiling():
    from agent.chat_completion_helpers import _consume_ephemeral_max_output
    from agent.turn_truncation import boosted_output_cap

    agent = SimpleNamespace(api_mode="chat_completions", max_tokens=4_000,
                            _ephemeral_max_output_tokens=None, _preflight_output_ceiling=5_000)
    agent._ephemeral_max_output_tokens = boosted_output_cap(agent, 5_000, 1)
    assert agent._ephemeral_max_output_tokens == 8_000
    assert _consume_ephemeral_max_output(agent) == 8_000


# ---- turn-scoped ceiling on a cached (gateway-reused) agent -------------------------------


@pytest.fixture()
def cached_agent():
    from unittest.mock import patch

    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
        )
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a._disable_streaming = True
    a.tool_delay = 0
    a.save_trajectories = False
    return a


def _stop_response():
    msg = SimpleNamespace(content="done", tool_calls=None, reasoning_content=None, reasoning=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="test/model", usage=None,
    )


def test_ceiling_cleared_at_turn_start_and_after_success(cached_agent):
    from unittest.mock import patch

    seen = []

    def _create(**kwargs):
        seen.append(cached_agent._preflight_output_ceiling)
        # Arm mid-turn, as the gate does on a fitting-but-tight request.
        cached_agent._preflight_output_ceiling = 1_234
        return _stop_response()

    cached_agent.client.chat.completions.create.side_effect = _create
    # A previous turn on this cached agent left a stale ceiling behind.
    cached_agent._preflight_output_ceiling = 999
    with (
        patch.object(cached_agent, "_persist_session"),
        patch.object(cached_agent, "_save_trajectory"),
        patch.object(cached_agent, "_cleanup_task_resources"),
    ):
        first = cached_agent.run_conversation("hello")
        second = cached_agent.run_conversation("again", conversation_history=first["messages"])

    assert first["completed"] is True and second["completed"] is True
    assert seen == [None, None]
    assert cached_agent._preflight_output_ceiling is None


def test_ceiling_cleared_on_early_return_exit(cached_agent, monkeypatch):
    import agent.conversation_loop as loop

    def _early_return(agent, *_a, **_k):
        agent._preflight_output_ceiling = 2_048
        return {"final_response": "x", "messages": [], "completed": False, "api_calls": 0,
                "compression_deferred": True, "failed": False}

    monkeypatch.setattr(loop, "_run_conversation_turn", _early_return)
    result = cached_agent.run_conversation("hello")
    assert result["compression_deferred"] is True
    assert cached_agent._preflight_output_ceiling is None


def test_ceiling_cleared_when_turn_raises(cached_agent, monkeypatch):
    import agent.conversation_loop as loop

    def _boom(agent, *_a, **_k):
        agent._preflight_output_ceiling = 2_048
        agent._ephemeral_max_output_tokens = 2_048
        agent._ephemeral_output_cap_is_retry_boost = True
        raise RuntimeError("boom")

    monkeypatch.setattr(loop, "_run_conversation_turn", _boom)
    with pytest.raises(RuntimeError):
        cached_agent.run_conversation("hello")
    assert cached_agent._preflight_output_ceiling is None
    assert cached_agent._ephemeral_max_output_tokens is None
    assert cached_agent._ephemeral_output_cap_is_retry_boost is False


def test_stale_ephemeral_cleared_at_turn_start(cached_agent):
    from unittest.mock import patch

    cached_agent._preflight_output_ceiling = 2_048
    cached_agent._ephemeral_max_output_tokens = 2_048
    cached_agent._ephemeral_output_cap_is_retry_boost = True
    cached_agent.client.chat.completions.create.side_effect = lambda **_k: _stop_response()
    with (
        patch.object(cached_agent, "_persist_session"),
        patch.object(cached_agent, "_save_trajectory"),
        patch.object(cached_agent, "_cleanup_task_resources"),
    ):
        result = cached_agent.run_conversation("hello")
    assert result["completed"] is True
    assert cached_agent._preflight_output_ceiling is None
    assert cached_agent._ephemeral_max_output_tokens is None
    assert cached_agent._ephemeral_output_cap_is_retry_boost is False


# ---- input_retained result contract ------------------------------------------------------


def test_input_retained_is_a_typed_gateway_layer_code():
    from agent.error_surface import build_error_surface_from_result
    from agent.turn_failure_copy import SITE_FAILURE_CODES, exit_reason_failure, retained_input_result

    result = retained_input_result([], 0)
    assert "input_retained" in SITE_FAILURE_CODES
    verdict = exit_reason_failure("input_retained")
    assert verdict is not None and verdict.reason == "input_retained"
    surface = build_error_surface_from_result(result)
    assert surface["code"] == "input_retained"
    assert surface["layer"] == "gateway"


def test_input_retained_copy_does_not_offer_disabled_compression():
    from agent.turn_failure_copy import retained_input_result

    result = retained_input_result([], 0, compression_enabled=False)
    assert "/compress" not in result["final_response"]
    assert "/new" in result["final_response"]


def test_gateway_keeps_session_on_input_retained():
    """No compression_exhausted flag, no overflow verdict: the gateway neither auto-resets
    the session nor drops the user's row from the transcript."""
    from gateway.run_turn import is_context_overflow_failure_result

    from agent.turn_failure_copy import retained_input_result

    result = retained_input_result([{"role": "user", "content": "x"}], 0)
    assert not result.get("compression_exhausted")
    assert not result.get("compression_deferred")
    assert is_context_overflow_failure_result(result, history_len=200) is False
