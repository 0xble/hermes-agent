"""Regression coverage for long-context recovery under compression-pool saturation."""

from types import SimpleNamespace

from agent import conversation_compression, turn_recovery
from agent.error_classifier import ClassifiedError, FailoverReason
from agent.turn_retry_state import TurnRetryState


def test_long_context_tier_pool_saturation_refunds_and_defers(monkeypatch):
    monkeypatch.setattr(turn_recovery.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(
        conversation_compression,
        "conversation_history_after_compression",
        lambda *_args: [],
    )

    for capped_context in (300_000, 200_000):
        messages = [{"role": "user", "content": "oversized request"}]
        agent = SimpleNamespace(
            session_id="long-context-pool-canary",
            _compression_blocked_transient=None,
            tools=[],
            _buffer_diagnostic_status=lambda *_args, **_kwargs: None,
            _flush_status_buffer=lambda: None,
            _compress_context=lambda *_args, **_kwargs: (
                setattr(agent, "_compression_blocked_transient", "pool_saturated")
                or (messages, "system")
            ),
        )
        monkeypatch.setattr(
            turn_recovery,
            "_cap_long_context_tier",
            lambda _agent, context=capped_context: context,
        )

        verdict = turn_recovery.route_classified_error(
            agent,
            Exception("Extra usage is required for long context requests"),
            ClassifiedError(reason=FailoverReason.long_context_tier, status_code=429),
            TurnRetryState(),
            error_msg="long context tier",
            error_context={},
            recovered_with_pool=False,
            base_url="https://example.invalid",
            model="test/model",
            messages=messages,
            api_messages=messages,
            system_message="system",
            active_system_prompt="system",
            conversation_history=[],
            retry_count=0,
            max_retries=3,
            compression_attempts=0,
            max_compression_attempts=1,
            api_call_count=1,
            effective_task_id="task",
        )

        assert verdict.action == "return"
        assert verdict.compression_attempts == 0
        result = verdict.result
        assert result is not None
        assert result["compression_deferred"] is True
        assert result["failed"] is False
