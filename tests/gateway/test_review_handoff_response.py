"""Intentional review handoffs survive agent finalization and gateway normalization."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.turn_finalizer import finalize_turn
from agent.turn_tool_round import run_tool_round
from gateway.config import Platform
from gateway.run import _normalize_empty_agent_response
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


def _agent_result(*, persistence_error=False, reason=None):
    agent = MagicMock()
    agent.model = "test-model"
    agent.provider = "test"
    agent.base_url = "http://unused"
    agent.session_id = "review-handoff"
    agent.max_iterations = 10
    agent.iteration_budget = SimpleNamespace(used=1, max_total=10, remaining=9)
    agent.context_compressor = SimpleNamespace(last_prompt_tokens=0)
    agent.quiet_mode = True
    agent.verbose_logging = False
    agent._tool_guardrail_halt_decision = None
    agent._incremental_persistence_failed = False
    agent._review_yield_requested = True
    agent._response_was_previewed = False
    agent._skill_nudge_interval = 0
    agent._iters_since_skill = 0
    agent._session_db = None
    agent._streamed_text = ""
    agent._turn_preflight_display_snapshot = None
    agent._turn_completion_explainer_enabled.return_value = False
    agent._drain_pending_steer.return_value = None
    agent._deduplicate_tool_calls.side_effect = lambda calls: calls
    agent._cap_delegate_task_calls.side_effect = lambda calls: calls
    agent._flush_messages_to_session_db.return_value = True
    agent._execute_tool_calls.side_effect = lambda _message, messages, *_args: messages.append(
        {"role": "tool", "tool_call_id": "review", "content": '{"status":"running"}'}
    )
    if persistence_error:
        agent._persist_session.side_effect = RuntimeError("session storage unavailable")
    messages = [{"role": "user", "content": "review"}]
    with patch("agent.turn_tool_round.validate_tool_calls", return_value=SimpleNamespace(
        action="run", mixed_invalid_batch=False
    )), patch("agent.turn_tool_round.stage_tool_call_message", return_value=({
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "review", "function": {"name": "review_changes"}}],
    }, True)):
        verdict = run_tool_round(
            agent, assistant_message=SimpleNamespace(tool_calls=[]), finish_reason="tool_calls",
            messages=messages, conversation_history=[], api_call_count=1,
            effective_task_id="parent", user_message="review", system_message="",
            active_system_prompt="", compression_attempts=0, max_compression_attempts=3,
            final_response="", failed=False, _turn_exit_reason=None,
            truncated_tool_call_retries=0,
        )
    assert verdict.action == "break"
    assert verdict._turn_exit_reason == "review_dispatched"
    assert verdict.final_response == ""
    return finalize_turn(
        agent, final_response=verdict.final_response, api_call_count=1,
        interrupted=False, failed=verdict.failed, messages=messages, conversation_history=[],
        effective_task_id="parent", turn_id="turn", user_message="review",
        original_user_message="review", _should_review_memory=False,
        _turn_exit_reason=reason or verdict._turn_exit_reason,
    )


def _gateway_result(result):
    class ResultAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
            self.session_id = kwargs["session_id"]
            self.tools = []

        def run_conversation(self, *args, **kwargs):
            return result

    gateway = MagicMock()
    gateway.config = SimpleNamespace(streaming=None)
    gateway._provider_routing = {}
    gateway._agent_cache_lock = None
    gateway._agent_cache = {}
    gateway._session_db = None
    gateway._prefill_messages = None
    gateway._pending_model_notes = {}
    gateway._pending_skills_reload_notes = {}
    gateway.session_store._entries = {}
    gateway._get_system_prompt_for_channel.return_value = None
    gateway._resolve_session_agent_runtime.return_value = ("test-model", {})
    gateway._resolve_session_reasoning_config.return_value = None
    gateway._resolve_session_service_tier.return_value = None
    gateway._resolve_turn_agent_config.return_value = {"model": "test-model", "runtime": {}}
    gateway._agent_config_signature.return_value = ("test",)
    gateway._extract_cache_busting_config.return_value = {}
    gateway._refresh_fallback_model.return_value = None
    gateway._consume_pending_native_image_paths.return_value = []
    gateway._consume_pending_turn_sidecar_notes.return_value = []
    gateway._is_telegram_topic_lane.return_value = False
    gateway._is_discord_auto_thread_lane.return_value = False
    gateway._is_relay_discord_channel_lane.return_value = False
    ctx = TurnContext(
        source=SessionSource(platform=Platform.LOCAL, chat_id="test", user_id="test"),
        message="review", history=[], session_id="review-handoff", session_key="test",
        user_config={}, AIAgent=ResultAgent, resolve_display_setting=lambda *args: False,
        _run_still_current=lambda: True, _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )
    return TurnRunner(gateway, ctx).run_sync()


@pytest.mark.parametrize("persistence_error, reason", [
    (False, "review_dispatched"), (True, "review_dispatched"), (False, "unknown"),
])
def test_review_handoff_crosses_agent_gateway_boundary(caplog, persistence_error, reason):
    caplog.set_level(logging.INFO)
    result = _agent_result(persistence_error=persistence_error, reason=reason)
    assert result["turn_exit_reason"] == reason
    assert bool(result.get("cleanup_errors")) is persistence_error
    gateway_result = _gateway_result(result)
    # run_sync performed the first normalization. The outer gateway turn normalizes again.
    response = _normalize_empty_agent_response(gateway_result, gateway_result["final_response"])
    assert bool(response) is (persistence_error or reason != "review_dispatched")
    if reason == "unknown":
        assert any("agent may appear stuck" in record.message for record in caplog.records)
    elif not persistence_error:
        assert not any("agent may appear stuck" in record.message for record in caplog.records)
        assert any("review_dispatched" in record.message and record.levelno == logging.INFO
                   for record in caplog.records)


@pytest.mark.parametrize("overrides, text, expected", [
    ({"turn_exit_reason": "unknown"}, "", "no response was generated"),
    ({"failed": True, "error": "review failed"}, "", "review failed"),
    ({"partial": True, "error": "incomplete"}, "", "Processing stopped"),
    ({"failed": True, "failure_reason": "session_persistence_failed:disk"}, "", "disk"),
    ({"interrupted": True, "api_calls": 0}, "", "interrupted before processing"),
    ({"interrupted": True}, "", ""),
    ({"api_calls": 0}, "", "wasn't processed"),
    ({"completed": False}, "", "no response was generated"),
    ({"error": "late error"}, "", "no response was generated"),
    ({"failure_reason": "session_persistence_failed:locked"}, "", "no response was generated"),
    ({"cleanup_errors": ["persist_session: locked"]}, "", "no response was generated"),
    ({}, "Actual answer", "Actual answer"),
    ({}, "[SILENT]", "[SILENT]"),
])
def test_review_reason_does_not_override_existing_outcomes(overrides, text, expected):
    result = {"turn_exit_reason": "review_dispatched", "completed": True, "api_calls": 1,
              **overrides}
    response = _normalize_empty_agent_response(result, text)
    assert expected in response if expected else response == ""
    assert _normalize_empty_agent_response(result, response) == response
