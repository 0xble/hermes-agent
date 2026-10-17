from types import SimpleNamespace
from unittest.mock import patch

from gateway.attended_model import AttendedResolution
from gateway.run_turn_runner import TurnRunner


def _ctx(config=None):
    return SimpleNamespace(
        user_config=config or {"model": {"attended": {"model": "opus", "provider": "anthropic"}}},
        source=None,
        session_key="session-1",
    )


def _runner(decision, runtime=None):
    runner = SimpleNamespace()
    runner._attended_resolution_for_session = lambda **_kwargs: decision
    runner._session_model_override = lambda _key: None
    runner._resolve_session_agent_runtime = lambda **_kwargs: runtime or (
        "opus", {"provider": "anthropic", "api_mode": "anthropic_messages"}
    )
    runner._resolve_session_reasoning_config = lambda **_kwargs: {"enabled": True, "effort": "medium"}
    state = SimpleNamespace(conversation=SimpleNamespace(last_resolved_model=""))
    runner._session_state = lambda _key: state
    runner._peek_session_state = lambda _key: state
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = _ctx()
    return turn, runner, state


def test_fallback_is_not_undone_when_desired_route_changes_mid_turn():
    decision = AttendedResolution("opus", "anthropic", "medium", "attended", True)
    turn, _runner_obj, _state = _runner(decision)
    agent = SimpleNamespace(
        model="fallback", provider="fallback-provider", api_mode="chat_completions",
        _gateway_attended_decision_key=("default", "nous", "xhigh", "default", False),
        _fallback_activated=True, _provider_fallback_active=False,
    )

    turn._apply_attended_before_api_call(agent)

    assert agent.model == "fallback"
    assert agent._gateway_attended_decision_key == decision.decision_key


def test_mid_turn_activation_switches_once_and_posts_one_notice():
    decision = AttendedResolution("opus", "anthropic", "medium", "attended", True)
    turn, runner, state = _runner(decision)
    notices = []
    agent = SimpleNamespace(
        model="default", provider="nous", api_mode="codex_responses",
        _gateway_attended_decision_key=("default", "nous", "xhigh", "default", False),
        _fallback_activated=False, _provider_fallback_active=False,
        reasoning_config={"enabled": True, "effort": "xhigh"},
        _emit_diagnostic_status=notices.append,
        context_compressor=None,
    )

    with patch("agent.agent_runtime_helpers.switch_model") as switch:
        turn._apply_attended_before_api_call(agent)
        turn._apply_attended_before_api_call(agent)

    switch.assert_called_once_with(
        agent, "opus", "anthropic", "", "", "anthropic_messages", None,
    )
    assert notices == ["↻ opus · medium"]
    assert state.conversation.last_resolved_model == "opus"


def test_compaction_failure_does_not_block_a_switch():
    turn = TurnRunner.__new__(TurnRunner)
    turn._ctx = _ctx({
        "model": {
            "attended": {"model": "opus", "provider": "anthropic"},
            "compact_before_switch_tokens": 10,
        }
    })
    compressor = SimpleNamespace(last_prompt_tokens=100, context_length=1000)
    agent = SimpleNamespace(
        context_compressor=compressor, compression_enabled=True, tools=[],
        _compress_context=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("compress failed")),
    )
    loop_state = SimpleNamespace(
        messages=[{"role": "user", "content": "large"}], active_system_prompt="",
        system_message="", effective_task_id="task", conversation_history=[],
    )

    # The method is intentionally fail-open: an exception is logged and the caller can switch.
    turn._compact_before_attended_switch(
        agent, loop_state, model="opus", runtime={"provider": "anthropic", "context_length": 1000},
    )
    assert loop_state.messages == [{"role": "user", "content": "large"}]
