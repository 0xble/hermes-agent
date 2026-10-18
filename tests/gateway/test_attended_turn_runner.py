from types import SimpleNamespace
from unittest.mock import patch
import sys

from gateway.attended_model import AttendedResolution
from gateway.session import Platform, SessionSource
from gateway.turn_context import TurnContext
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
    turn, _, state = _runner(decision)
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
    assert notices == ["⇄ Switched to Opus while you're active."]
    assert state.conversation.last_resolved_model == "opus"


def test_mid_turn_idle_expiry_switches_back_once_with_one_notice():
    previous = AttendedResolution(
        "claude-opus-5-5", "anthropic", "medium", "attended", True, idle_model="gpt-6.1-sol",
    )
    decision = AttendedResolution(
        "gpt-6.1-sol", "nous", "xhigh", "default", False, switched_back=True,
    )
    turn, _, state = _runner(
        decision, runtime=("gpt-6.1-sol", {"provider": "nous", "api_mode": "codex_responses"}),
    )
    notices = []
    agent = SimpleNamespace(
        model="claude-opus-5-5", provider="anthropic", api_mode="anthropic_messages",
        _gateway_attended_decision=previous,
        _gateway_attended_decision_key=previous.decision_key,
        _fallback_activated=False, _provider_fallback_active=False,
        reasoning_config={"enabled": True, "effort": "medium"},
        _emit_diagnostic_status=notices.append,
        context_compressor=None,
    )

    with patch("agent.agent_runtime_helpers.switch_model") as switch:
        turn._apply_attended_before_api_call(agent)
        turn._apply_attended_before_api_call(agent)

    switch.assert_called_once_with(
        agent, "gpt-6.1-sol", "nous", "", "", "codex_responses", None,
    )
    assert notices == ["⇄ Back to GPT-6.1 Sol after 15m idle."]
    assert state.conversation.last_resolved_model == "gpt-6.1-sol"


def test_oversized_default_context_keeps_attended_route_without_compaction():
    previous = AttendedResolution(
        "claude-opus-5-5", "anthropic", "medium", "attended", True, idle_model="gpt-6.1-sol",
    )
    decision = AttendedResolution(
        "gpt-6.1-sol", "nous", "xhigh", "default", False, switched_back=True,
    )
    turn, _, state = _runner(
        decision, runtime=("gpt-6.1-sol", {"provider": "nous", "api_mode": "codex_responses", "context_length": 272000}),
    )
    state.conversation.last_attended_decision = previous
    notices = []
    compressed = []
    compressor = SimpleNamespace(
        preview_threshold_tokens=lambda *_args: 204000,
    )
    agent = SimpleNamespace(
        model="claude-opus-5-5", provider="anthropic", api_mode="anthropic_messages",
        _gateway_attended_decision=previous,
        _gateway_attended_decision_key=previous.decision_key,
        _fallback_activated=False, _provider_fallback_active=False,
        reasoning_config={"enabled": True, "effort": "medium"},
        _emit_diagnostic_status=notices.append,
        context_compressor=compressor,
        _compress_context=lambda *_args, **_kwargs: compressed.append(True),
    )
    loop_state = SimpleNamespace(request_pressure_tokens=204000, messages=[])

    with patch("agent.agent_runtime_helpers.switch_model") as switch:
        turn._apply_attended_before_api_call(agent, loop_state)

    switch.assert_not_called()
    assert notices == []
    assert compressed == []
    assert state.conversation.last_attended_decision is previous


def test_switch_only_compaction_path_was_removed():
    assert not hasattr(TurnRunner, "_compact_before_attended_switch")


def test_turn_start_attended_notice_reaches_status_callback_once(monkeypatch):
    state = SimpleNamespace(
        conversation=SimpleNamespace(
            pending_attended_notice="⇄ Switched to Opus while you're active.",
            last_attended_decision=None,
        )
    )
    runner = SimpleNamespace(
        _provider_routing={},
        _service_tier=None,
        _resolve_session_agent_runtime=lambda **_kwargs: (
            "opus", {"provider": "anthropic", "api_mode": "anthropic_messages"}
        ),
        _resolve_session_reasoning_config=lambda **_kwargs: None,
        _resolve_session_service_tier=lambda **_kwargs: None,
        _resolve_turn_agent_config=lambda _message, model, runtime: {"model": model, "runtime": runtime},
        _session_state=lambda _key: state,
    )
    ctx = TurnContext(
        source=SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u"),
        message="hi", history=[], session_id="sid", session_key="session-1", user_config={},
    )
    turn = TurnRunner(runner, ctx)
    agent = SimpleNamespace(
        model="opus", tools=[], context_compressor=None,
        session_prompt_tokens=0, session_completion_tokens=0,
    )
    notices = []

    monkeypatch.setattr(turn, "_combined_ephemeral_prompt", lambda: "")
    monkeypatch.setattr(turn, "_setup_stream_consumer", lambda _platform: (None, None, None, False))
    monkeypatch.setattr(turn, "_resolve_turn_agent", lambda *args: (agent, False))
    monkeypatch.setattr(turn, "_wire_turn_agent_callbacks", lambda *_args: (
        setattr(agent, "status_callback", lambda _kind, text: notices.append(text)),
        setattr(agent, "_emit_diagnostic_status", lambda text: agent.status_callback("lifecycle", text)),
    ))
    monkeypatch.setattr(turn, "_load_turn_history", lambda *_args: ([], None, []))
    monkeypatch.setattr(turn, "_prepare_turn_message", lambda *_args: ("hi", 0.0))
    monkeypatch.setattr(turn, "_run_conversation_with_approval", lambda *_args: {
        "final_response": "ok", "messages": [], "api_calls": 1,
    })
    monkeypatch.setattr(turn, "_finish_stream_consumer", lambda result, *_args: result)
    monkeypatch.setattr(turn, "_append_auto_media_tags", lambda final_response, *_args: final_response)
    monkeypatch.setattr(turn, "_sync_session_after_run", lambda *_args: (False, "sid", 0))

    with patch.dict(sys.modules, {"gateway.run": SimpleNamespace(_current_max_iterations=lambda: 1)}):
        turn.run_sync()

    assert notices == ["⇄ Switched to Opus while you're active."]
    assert state.conversation.pending_attended_notice is None
