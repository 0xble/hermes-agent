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
        context_compressor=_compressor(awaiting_real_usage_after_compression=True),
    )
    agent._compress_context = MagicMock()
    _fits_when(monkeypatch, lambda *_: False, wire_tokens=180_000)
    v = _gate(agent, pressure=180_000)
    assert v.action == "fallthrough"
    agent._compress_context.assert_not_called()


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
        raise RuntimeError("boom")

    monkeypatch.setattr(loop, "_run_conversation_turn", _boom)
    with pytest.raises(RuntimeError):
        cached_agent.run_conversation("hello")
    assert cached_agent._preflight_output_ceiling is None


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
