"""Regression for transient pre-API compression-pool saturation.

A full shared pool is temporary capacity pressure, not evidence that the
session is incompressible. The preflight attempt must be refunded so a later
pass can compress after a worker frees.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import agent.conversation_compression as compression
from agent.turn_preflight import PreflightGateVerdict, run_preflight_compression


_MESSAGES = [
    {"role": "user", "content": "first"},
    {"role": "assistant", "content": "second"},
]


def _compressor():
    return SimpleNamespace(
        context_length=100_000,
        threshold_tokens=5_000,
        protect_first_n=0,
        protect_last_n=0,
        should_compress=lambda _tokens: True,
        should_defer_preflight_to_real_usage=lambda _tokens: False,
        get_active_compression_failure_cooldown=lambda: None,
    )


def _verdict(messages):
    return PreflightGateVerdict(
        action="",
        pending_moa_prepared_request=None,
        messages=messages,
        active_system_prompt="sys",
        conversation_history=list(messages),
        api_call_count=1,
        compression_attempts=0,
        final_response=None,
        failed=False,
        _turn_exit_reason=None,
        _compression_timeout_exhausted=False,
        _preflight_compression_blocked=False,
        _provider_overflow_recovery_pending=False,
        _last_preflight_pressure=None,
    )


def test_saturated_pool_refunds_pre_api_attempt_and_recovers(monkeypatch):
    """The pool fills, the refusal defers, then the next pass commits compression."""
    import agent.conversation_loop as loop
    import agent.turn_preflight as preflight

    monkeypatch.setattr(preflight, "ensure_compression_feasibility_checked", lambda *_a, **_k: None)
    monkeypatch.setattr(preflight, "_review_fork_first_request_pending", lambda _agent: False)
    monkeypatch.setattr(preflight, "_reset_retry_state_after_compaction", lambda _agent: None)
    monkeypatch.setattr(preflight, "conversation_history_after_compression", lambda _a, m, _h=None: list(m))
    monkeypatch.setattr(preflight, "automatic_compaction_status_message", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "_maybe_grow_local_window", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "_should_skip_model_call_for_reference_handoff", lambda *_a: False)

    agent = SimpleNamespace(
        compression_enabled=True,
        context_compressor=_compressor(),
        model="test/model",
        session_id="pool-saturation-preflight",
        _compression_skipped_due_to_lock=None,
        _compression_blocked_transient=None,
        _last_compression_timed_out=False,
        _request_pressure_anchored=True,
        iteration_budget=SimpleNamespace(refund=MagicMock()),
        _persist_session=MagicMock(),
        _emit_status=MagicMock(),
        _warn_context_overflow_blocked=MagicMock(),
    )

    compressed = [{"role": "user", "content": "summary"}]
    saturated = {"value": True}

    def _compress_context(messages, _system_message, **_kwargs):
        # The facade clears per-attempt signals before entering the shared
        # timeout wrapper. Mirror that boundary in this focused preflight test.
        agent._compression_blocked_transient = None
        return compression.run_compress_context_with_progress_timeout(
            worker=(
                lambda _fence: (messages, "sys")
                if saturated["value"]
                else (compressed, "sys")
            ),
            messages=messages,
            system_prompt_fallback="sys",
            idle_timeout_seconds=1.0,
            total_ceiling_seconds=2.0,
            telemetry_agent=agent,
        )

    agent._compress_context = _compress_context
    held = []
    try:
        for _ in range(compression._COMPRESS_EXECUTOR_MAX_WORKERS):
            assert compression._try_admit_compression_job()
            held.append(True)

        first = run_preflight_compression(
            agent,
            _verdict(list(_MESSAGES)),
            compressor=agent.context_compressor,
            request_pressure_tokens=9_000,
            provider_overflow_preflight=False,
            defer_preflight=lambda _tokens: False,
            moa_prepared_request=None,
            system_message="sys",
            user_message="ask",
            max_compression_attempts=3,
            effective_task_id="task",
        )
        assert first.compression_attempts == 0
        assert first._last_preflight_pressure is None
        assert first._preflight_compression_blocked is False
        # A deferred pass still proceeds to the provider, so its API call is not refunded.
        assert agent.iteration_budget.refund.call_count == 0
        assert agent._compression_blocked_transient == "pool_saturated"

        for _ in held:
            compression._release_compression_admission()
        held.clear()
        saturated["value"] = False

        second = run_preflight_compression(
            agent,
            _verdict(list(_MESSAGES)),
            compressor=agent.context_compressor,
            request_pressure_tokens=9_000,
            provider_overflow_preflight=False,
            defer_preflight=lambda _tokens: False,
            moa_prepared_request=None,
            system_message="sys",
            user_message="ask",
            max_compression_attempts=3,
            effective_task_id="task",
        )
        assert second.messages == compressed
        assert second.compression_attempts == 1
        assert second._last_preflight_pressure == 9_000
        assert agent.iteration_budget.refund.call_count == 1
    finally:
        for _ in held:
            compression._release_compression_admission()
