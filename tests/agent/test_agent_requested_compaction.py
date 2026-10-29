"""Agent-requested compact_context gates and safe-boundary behavior."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from agent.compact_context import (
    clear_pending_compaction,
    commit_compaction,
    release_pending_compaction,
    reset_compaction_request_turn,
    take_pending_compaction,
)
from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from agent.turn_preflight import compress_after_tool_results


def _agent(*, current=500, threshold=1000, enabled=True, api_mode=""):
    compressor = SimpleNamespace(
        threshold_tokens=threshold,
        last_prompt_tokens=current,
        awaiting_real_usage_after_compression=False,
        should_compress=lambda _tokens: False,
    )
    return SimpleNamespace(
        context_compressor=compressor, compression_enabled=enabled,
        api_mode=api_mode,
        compression_agent_request_floor_ratio=0.30,
        compression_agent_request_cooldown_api_calls=10,
        max_compression_attempts=3, _compression_attempts=0, _api_call_count=1, session_api_calls=1,
        _session_messages=[{"role": "user", "content": "x"}],
    )


def _call(agent, **args):
    raw = INLINE_TOOL_EXECUTORS["compact_context"](
        agent, args, InlineToolContext(effective_task_id="task", tool_call_id="call")
    )
    return json.loads(raw)


def test_result_always_reports_current_tokens_and_threshold_for_accept_and_refusal():
    agent = _agent(current=500, threshold=1000)
    accepted = _call(agent, reason="phase complete", focus="decision X")
    assert accepted["accepted"] is True
    assert accepted["state"] == "accepted"
    assert accepted["current_context_tokens"] == 500
    assert accepted["auto_compress_threshold_tokens"] == 1000

    refused = _call(agent, reason="duplicate")
    assert refused["accepted"] is False
    assert refused["current_context_tokens"] == 500
    assert refused["auto_compress_threshold_tokens"] == 1000


def test_accepted_request_compacts_below_automatic_threshold_and_preserves_focus():
    agent = _agent(current=500, threshold=1000)
    assert _call(agent, reason="phase complete", focus="keep decision X")["accepted"]
    calls = []

    def compress(messages, system, **kwargs):
        calls.append(kwargs)
        return messages + [{"role": "assistant", "content": "summary"}], system

    agent._compress_context = compress
    messages = [{"role": "user", "content": "current"}]
    verdict = compress_after_tool_results(
        agent, messages=messages, system_message="sys", user_message="current",
        active_system_prompt="sys", conversation_history=None, compression_attempts=0,
        max_compression_attempts=3, effective_task_id="task", final_response=None,
        turn_exit_reason=None, current_turn_user_idx=0,
    )
    assert len(calls) == 1
    assert calls[0]["trigger"] == "agent_requested"
    assert calls[0]["focus_topic"] == "keep decision X"
    assert agent._pending_agent_compaction is None
    assert verdict.compression_attempts == 1


def test_gate_refusals_include_usage():
    for agent, expected in [
        (_agent(enabled=False), "disabled"),
        (_agent(current=100, threshold=1000), "below"),
    ]:
        result = _call(agent, reason="test")
        assert result["accepted"] is False
        assert expected in result["reason"]
        assert isinstance(result["current_context_tokens"], int)
        assert isinstance(result["auto_compress_threshold_tokens"], int)


def test_codex_app_server_refuses_focus_unsafe_agent_request():
    agent = _agent(api_mode="codex_app_server")
    result = _call(agent, reason="phase complete", focus="preserve decision X")
    assert result["accepted"] is False
    assert result["state"] == "refused"
    assert "unsupported" in result["reason"]
    assert getattr(agent, "_pending_agent_compaction", None) is None


def test_turn_reset_clears_pending_request():
    agent = _agent()
    assert _call(agent, reason="phase complete")["accepted"]
    reset_compaction_request_turn(agent)
    assert agent._pending_agent_compaction is None
    assert agent._agent_requested_compaction_this_turn is False


def test_duplicate_requests_are_atomic_across_concurrent_tool_paths():
    agent = _agent()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _call(agent, reason="phase complete"), range(8)))
    assert sum(result["accepted"] for result in results) == 1
    assert {result["state"] for result in results} == {"accepted", "refused"}


def test_transient_or_lock_defer_preserves_request_until_next_boundary():
    agent = _agent()
    assert _call(agent, reason="phase complete", focus="keep path.py")["accepted"]
    claimed = take_pending_compaction(agent)
    assert claimed is not None
    assert claimed["focus"] == "keep path.py"
    release_pending_compaction(agent)
    assert agent._pending_agent_compaction == claimed
    assert agent._agent_compaction_request_state == "deferred"


def test_exhaustion_and_fresh_usage_gate_refuse_requests():
    agent = _agent()
    agent._compression_attempts = agent.max_compression_attempts
    exhausted = _call(agent, reason="phase complete")
    assert exhausted["accepted"] is False
    assert exhausted["state"] == "refused"
    assert getattr(agent, "_pending_agent_compaction", None) is None

    agent = _agent()
    agent.context_compressor.awaiting_real_usage_after_compression = True
    waiting = _call(agent, reason="phase complete")
    assert waiting["accepted"] is False
    assert waiting["state"] == "refused"


def test_commit_arms_fresh_usage_and_clear_removes_request():
    agent = _agent()
    assert _call(agent, reason="phase complete")["accepted"]
    commit_compaction(agent)
    assert agent._agent_compaction_request_state == "committed"
    assert agent.context_compressor.awaiting_real_usage_after_compression is True
    assert agent._pending_agent_compaction is None

    agent = _agent()
    assert _call(agent, reason="phase complete")["accepted"]
    clear_pending_compaction(agent)
    assert agent._pending_agent_compaction is None


def test_cooldown_counts_session_api_calls_across_turns():
    agent = _agent()
    agent.session_api_calls, agent._api_call_count = 15, 15
    assert _call(agent, reason="phase complete")["accepted"]
    reset_compaction_request_turn(agent)
    # Next turn: the per-turn counter restarts but the session counter keeps going.
    agent._api_call_count, agent.session_api_calls = 1, 19
    assert "cooldown" in _call(agent, reason="too soon")["reason"]
    agent._api_call_count, agent.session_api_calls = 2, 25
    assert _call(agent, reason="next phase")["accepted"]


def test_zero_floor_ratio_disables_the_floor():
    agent = _agent(current=10, threshold=1000)
    agent.compression_agent_request_floor_ratio = 0
    assert _call(agent, reason="phase complete")["accepted"]


def test_shared_metrics_keeps_agent_requested_out_of_other():
    from hermes_cli.observability.shared_metrics_fields import compression_fields

    fields = compression_fields(trigger="agent_requested", outcome="success", tokens_before=1, context_length=10)
    assert fields["trigger"] == "manual"
