from types import SimpleNamespace
from unittest.mock import patch
import sys

import pytest

from gateway.attended_model import AttendedResolution
from gateway.session import Platform, SessionSource
from gateway.turn_context import TurnContext
from gateway.run_turn_runner import TurnRunner
from agent import conversation_loop


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
    # Attended cards bypass the temporary status rail; capture the persistent sink.
    turn.attended_notices = []
    turn._emit_persistent_attended_notice = turn.attended_notices.append
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


def test_committed_api_boundary_persists_attended_route_marker():
    decision = AttendedResolution("opus", "anthropic", "medium", "attended", True)
    turn, runner, _state = _runner(decision)
    marked = []
    runner.session_store = SimpleNamespace(
        mark_attended_route_active=lambda key, active: marked.append((key, active)),
    )
    agent = SimpleNamespace(
        model="opus", provider="anthropic", api_mode="anthropic_messages",
        _gateway_attended_decision_key=decision.decision_key,
        _fallback_activated=False, _provider_fallback_active=False,
        reasoning_config={"enabled": True, "effort": "medium"},
        context_compressor=None,
    )

    turn._apply_attended_before_api_call(agent)

    assert marked == [("session-1", True)]


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
    assert turn.attended_notices == ["⇄ Switched to Opus while you're active."]
    assert notices == []
    assert state.conversation.last_resolved_model == "opus"


@pytest.mark.parametrize("previous_key", [None, ("default", "nous", "xhigh", "default", False)])
def test_boundary_does_not_announce_an_already_selected_attended_model(previous_key):
    decision = AttendedResolution("opus", "anthropic", "medium", "attended", True)
    turn, _, _ = _runner(decision)
    agent = SimpleNamespace(
        model="opus", provider="anthropic", api_mode="anthropic_messages",
        _gateway_attended_decision_key=previous_key,
        reasoning_config={"enabled": True, "effort": "medium"},
    )

    with patch("agent.agent_runtime_helpers.switch_model") as switch:
        turn._apply_attended_before_api_call(agent, commit=False)
        turn._apply_attended_before_api_call(agent)

    switch.assert_not_called()
    assert turn.attended_notices == []


@pytest.mark.parametrize("route_active", [False, True])
def test_queued_turn_stages_notice_only_after_a_committed_idle_route(route_active):
    from gateway.run_turn import GatewayTurnMixin

    decision = AttendedResolution("opus", "anthropic", "medium", "attended", True)
    turn, runner, state = _runner(decision)
    state.conversation.pending_attended_notice = None
    # The committed API-route marker survives queued turns and worker handoffs. Model
    # resolution alone can still have a stale model snapshot from before the switch.
    runner._load_attended_route_active = lambda _key: route_active
    runner._handoff_attended_state = lambda _key, conversation: vars(state.conversation).update(conversation)
    config = {"model": {"default": "default", "attended": {"model": "opus", "provider": "anthropic"}}}

    for _ in range(2):
        GatewayTurnMixin._attended_route_for_session(
            runner, source=None, session_key="session-1", user_config=config,
            override=None, last_model="default",
        )

    assert bool(state.conversation.pending_attended_notice) is (not route_active)


@pytest.mark.parametrize("route_active", [False, True])
def test_snapshot_route_state_is_used_without_a_loop_store_read(route_active):
    """Loop callers pass the off-loop snapshot's route marker; reading the store here would take
    the session-store lock on the gateway loop (live loop-guard hit, 2026-10-25)."""
    from gateway.run_turn import GatewayTurnMixin

    decision = AttendedResolution("opus", "anthropic", "medium", "attended", True)
    turn, runner, state = _runner(decision)
    state.conversation.pending_attended_notice = None

    def _loop_read(_key):
        raise AssertionError("route marker read on the loop despite a snapshot value")

    runner._load_attended_route_active = _loop_read
    runner._handoff_attended_state = lambda _key, conversation: vars(state.conversation).update(conversation)
    config = {"model": {"default": "default", "attended": {"model": "opus", "provider": "anthropic"}}}

    GatewayTurnMixin._attended_route_for_session(
        runner, source=None, session_key="session-1", user_config=config,
        override=None, last_model="default", route_active=route_active,
    )

    assert bool(state.conversation.pending_attended_notice) is (not route_active)


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
    assert turn.attended_notices == ["⇄ Back to GPT-6.1 Sol after 15m idle."]
    assert notices == []
    assert state.conversation.last_resolved_model == "gpt-6.1-sol"
    # The reasoning lookup is stubbed to the stale attended effort; the committed decision wins.
    assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}


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
    assert notices == [] and turn.attended_notices == []
    assert compressed == []
    assert state.conversation.last_attended_decision is previous


def test_switch_only_compaction_path_was_removed():
    assert not hasattr(TurnRunner, "_compact_before_attended_switch")


def test_new_session_first_attended_turn_has_no_switch_in_notice():
    decision = AttendedResolution("opus", "anthropic", "medium", "attended", True, new_session=True)
    turn, runner, _state = _runner(decision)
    runner.session_store = SimpleNamespace(get_attended_route_active=lambda _key: None)
    notices = []
    agent = SimpleNamespace(
        model="default", provider="nous", api_mode="codex_responses",
        _gateway_attended_decision=None,
        _gateway_attended_decision_key=None,
        _fallback_activated=False, _provider_fallback_active=False,
        reasoning_config={"enabled": True, "effort": "xhigh"},
        _emit_diagnostic_status=notices.append,
        context_compressor=None,
    )

    with patch("agent.agent_runtime_helpers.switch_model"):
        turn._apply_attended_before_api_call(agent)

    assert notices == [] and turn.attended_notices == []


@pytest.mark.parametrize("notice", [
    "⇄ Switched to Opus while you're active.",
    "⇄ Back to GPT-6.1 Sol, model pick expired.",
])
def test_turn_start_attended_notice_is_delivered_persistently_once(monkeypatch, notice):
    state = SimpleNamespace(
        conversation=SimpleNamespace(
            pending_attended_notice=notice,
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
    persistent = []
    monkeypatch.setattr(turn, "_emit_persistent_attended_notice", persistent.append)

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
        turn.run_sync()

    assert persistent == [notice]
    assert notices == []
    assert state.conversation.pending_attended_notice is None


def test_conversation_loop_orders_route_hooks_around_destination_preflight(monkeypatch):
    """Route changes use the real loop seam: assemble/preflight see the active route,
    and a post-preflight boundary switch rebuilds and fits for its destination route."""
    events = []
    ctx = SimpleNamespace(
        user_message="hello", original_user_message="hello", messages=[{"role": "user", "content": "hello"}],
        conversation_history=[], active_system_prompt="system", effective_task_id="task", turn_id="turn",
        should_review_memory=False, plugin_user_context="", ext_prefetch_cache="", current_turn_user_idx=0,
        preflight_compression_blocked=False,
    )
    budget = SimpleNamespace(remaining=1)
    agent = SimpleNamespace(
        model="default", provider="default-provider", api_mode="default-mode", max_iterations=1,
        max_compression_attempts=3, iteration_budget=budget, _budget_grace_call=False,
        _api_max_retries=0, _try_refresh_env_client_credentials=lambda: None,
    )

    def before(_state):
        events.append(("before", agent.model))
        agent.model, agent.provider, agent.api_mode = "attended", "attended-provider", "attended-mode"

    def after(_state):
        events.append(("after", agent.model))
        agent.model, agent.provider, agent.api_mode = "default", "default-provider", "default-mode"

    agent._gateway_before_api_call = before
    agent._gateway_after_preflight = after

    def phase(fn, _agent, _state, **_extra):
        name = fn.__name__
        if name == "begin_iteration":
            _state.api_call_count = 1
        if name == "assemble_api_request":
            events.append(("assemble", _agent.model))
        elif name == "run_preflight_gate":
            events.append(("preflight", _agent.model))
            _agent._preflight_output_ceiling = 111 if _agent.model == "attended" else 222
        elif name == "apply_retry_restarts":
            return SimpleNamespace(action="break")
        elif name == "announce_api_call":
            return SimpleNamespace(action="fallthrough")
        return SimpleNamespace(action="fallthrough")

    monkeypatch.setattr(conversation_loop, "begin_fast_mode_turn", lambda *_args: None)
    monkeypatch.setattr(conversation_loop, "build_turn_context", lambda *_args, **_kwargs: ctx)
    monkeypatch.setattr("agent.voice_turn_route.begin_voice_turn_route", lambda _agent, _messages, prompt: prompt)
    monkeypatch.setattr(conversation_loop, "play_prelude", lambda *_args: ("continue", None))
    monkeypatch.setattr(conversation_loop, "_run_phase", phase)
    def finalize(agent, *, final_response, api_call_count, interrupted, failed, messages,
                 conversation_history, effective_task_id, turn_id, user_message,
                 original_user_message, _should_review_memory, _turn_exit_reason,
                 _pending_verification_response=None, _pending_verification_response_previewed=False):
        return {"completed": True}
    monkeypatch.setattr(conversation_loop, "finalize_turn", finalize)

    result = conversation_loop._run_conversation_turn(agent, "hello")

    assert result == {"completed": True}
    assert events == [
        ("before", "default"),
        ("assemble", "attended"),
        ("preflight", "attended"),
        ("after", "attended"),
        ("assemble", "default"),
        ("preflight", "default"),
    ]
    assert agent._preflight_output_ceiling == 222


import asyncio
import logging

import pytest


@pytest.mark.parametrize("suppressed", [False, True])
def test_attended_notice_is_a_permanent_message_not_a_cleanup_bubble(monkeypatch, suppressed):
    """With cleanup_progress on, temp status bubbles are deleted after the final reply; the
    attended switch cards must survive, so they ride the platform-notice path, untracked."""
    from gateway import run

    sent = []

    class Adapter:
        config = SimpleNamespace(extra={})

        async def send(self, chat_id, content, **kwargs):
            sent.append((chat_id, content))
            return SimpleNamespace(success=True, message_id=str(len(sent)))

    adapter = Adapter()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="u", thread_id="7")
    gateway = object.__new__(run.GatewayRunner)
    gateway.config = None
    gateway._delivery_adapter_for = lambda _source: adapter
    gateway._thread_metadata_for_source = lambda s: {"thread_id": s.thread_id}
    ctx = TurnContext(
        source=source, _run_still_current=lambda: True, _cleanup_progress=True,
        _status_adapter=adapter, _status_chat_id="chat",
        user_config={"display": {"suppress_warning_notifications": suppressed}},
    )
    turn = TurnRunner(gateway, ctx)
    monkeypatch.setattr(run, "safe_schedule_threadsafe", lambda coro, *a, **k: asyncio.run(coro))

    turn._emit_persistent_attended_notice("⇄ Back to GPT-6.1 Sol after 15m idle.")

    assert sent == ([] if suppressed else [("chat", "⇄ Back to GPT-6.1 Sol after 15m idle.")])
    assert ctx._cleanup_msg_ids == []


def test_idle_switch_back_decision_log_carries_destination_effort(caplog):
    from gateway.run_turn import GatewayTurnMixin

    runner = GatewayTurnMixin.__new__(GatewayTurnMixin)

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        runner._log_attended_decision("session-1", None, action="switch", reason="idle", effort="xhigh")

    assert "action=switch reason=idle effort=xhigh" in caplog.text


@pytest.mark.parametrize("config,label", [
    ({"enabled": True, "effort": "xhigh"}, "xhigh"),
    ({"enabled": False}, "none"),
    (None, "none"),
])
def test_effort_log_label(config, label):
    from agent.reasoning_effort import effort_log_label

    assert effort_log_label(config) == label


_SILENT = {"model": {"default": "default", "attended": {"model": "opus", "provider": "anthropic", "notices": False}}}


def test_notices_false_switches_mid_turn_without_a_card():
    decision = AttendedResolution("opus", "anthropic", "medium", "attended", True)
    turn, _, state = _runner(decision)
    turn._ctx = _ctx(_SILENT)
    agent = SimpleNamespace(
        model="default", provider="nous", api_mode="codex_responses",
        _gateway_attended_decision_key=("default", "nous", "xhigh", "default", False),
        _fallback_activated=False, _provider_fallback_active=False,
        reasoning_config={"enabled": True, "effort": "xhigh"},
        _emit_diagnostic_status=lambda _m: None,
        context_compressor=None,
    )

    with patch("agent.agent_runtime_helpers.switch_model") as switch:
        turn._apply_attended_before_api_call(agent)

    switch.assert_called_once()
    assert turn.attended_notices == []
    assert state.conversation.last_resolved_model == "opus"


def test_notices_false_switches_back_without_a_card():
    previous = AttendedResolution(
        "claude-opus-5-5", "anthropic", "medium", "attended", True, idle_model="gpt-6.1-sol",
    )
    decision = AttendedResolution("gpt-6.1-sol", "nous", "xhigh", "default", False, switched_back=True)
    turn, _, _ = _runner(decision, runtime=("gpt-6.1-sol", {"provider": "nous", "api_mode": "codex_responses"}))
    turn._ctx = _ctx(_SILENT)
    agent = SimpleNamespace(
        model="claude-opus-5-5", provider="anthropic", api_mode="anthropic_messages",
        _gateway_attended_decision=previous, _gateway_attended_decision_key=previous.decision_key,
        _fallback_activated=False, _provider_fallback_active=False,
        reasoning_config={"enabled": True, "effort": "medium"},
        _emit_diagnostic_status=lambda _m: None,
        context_compressor=None,
    )

    with patch("agent.agent_runtime_helpers.switch_model") as switch:
        turn._apply_attended_before_api_call(agent)

    switch.assert_called_once()
    assert turn.attended_notices == []


def test_notices_false_stages_no_queued_turn_card():
    from gateway.run_turn import GatewayTurnMixin

    decision = AttendedResolution("opus", "anthropic", "medium", "attended", True)
    _, runner, state = _runner(decision)
    state.conversation.pending_attended_notice = None
    runner._load_attended_route_active = lambda _key: False
    runner._handoff_attended_state = lambda _key, conversation: vars(state.conversation).update(conversation)

    route = GatewayTurnMixin._attended_route_for_session(
        runner, source=None, session_key="session-1", user_config=_SILENT,
        override=None, last_model="default",
    )

    assert route is not None
    assert state.conversation.pending_attended_notice is None


def test_notices_must_be_a_boolean():
    from gateway.attended_model import parse_attended_config

    assert parse_attended_config(_SILENT).notices is False
    assert parse_attended_config({"model": {"attended": {"model": "opus"}}}).notices is True
    assert parse_attended_config({"model": {"attended": {"model": "opus", "notices": "no"}}}).notices is True
