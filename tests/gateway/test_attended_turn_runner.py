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


def test_compaction_reanchors_current_user_index():
    from unittest.mock import patch

    turn = TurnRunner.__new__(TurnRunner)
    turn._ctx = _ctx({
        "model": {
            "attended": {"model": "opus", "provider": "anthropic"},
            "compact_before_switch_tokens": 10,
        }
    })
    compressor = SimpleNamespace(last_prompt_tokens=100, context_length=1000)
    compressed = [
        {"role": "user", "content": "older"},
        {"role": "user", "content": "current"},
    ]
    agent = SimpleNamespace(
        context_compressor=compressor, compression_enabled=True, tools=[],
        _compress_context=lambda *args, **kwargs: (compressed, ""),
    )
    loop_state = SimpleNamespace(
        messages=[{"role": "user", "content": "current"}], active_system_prompt="",
        system_message="", effective_task_id="task", conversation_history=[],
        user_message="current", current_turn_user_idx=0,
    )

    with patch("agent.conversation_compression.conversation_history_after_compression", return_value=[]):
        turn._compact_before_attended_switch(
            agent, loop_state, model="opus", runtime={"provider": "anthropic", "context_length": 1000},
        )

    assert loop_state.current_turn_user_idx == 1
    assert agent._persist_user_message_idx == 1


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


def test_turn_start_attended_notice_reaches_status_callback_once(monkeypatch):
    state = SimpleNamespace(
        conversation=SimpleNamespace(
            pending_attended_notice="↻ opus · medium",
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

    assert notices == ["↻ opus · medium"]
    assert state.conversation.pending_attended_notice is None
