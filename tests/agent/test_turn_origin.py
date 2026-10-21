from types import SimpleNamespace

from agent.turn_context import _collect_pre_llm_call_context


def test_pre_llm_call_receives_turn_origin(monkeypatch):
    seen = {}

    def invoke_hook(name, **kwargs):
        seen["name"] = name
        seen.update(kwargs)
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke_hook)
    agent = SimpleNamespace(session_id="session", model="model", _persist_disabled=False)
    assert _collect_pre_llm_call_context(
        agent,
        effective_task_id="task",
        turn_id="turn",
        original_user_message="hello",
        messages=[],
        conversation_history=[],
        turn_origin="user",
    ) == ""
    assert seen["name"] == "pre_llm_call"
    assert seen["turn_origin"] == "user"


def _capture_turn_origin(monkeypatch):
    from agent import conversation_loop

    seen = {}

    def run_turn(_agent, _message, **kwargs):
        seen["turn_origin"] = kwargs["turn_origin"]
        return {"messages": []}

    monkeypatch.setattr(conversation_loop, "_run_conversation_turn", run_turn)
    monkeypatch.setattr(conversation_loop, "_close_durable_failed_turn", lambda *_a: None)
    monkeypatch.setattr("agent.turn_context.export_current_turn_boundary", lambda _a, result, _m: result)
    monkeypatch.setattr("agent.voice_turn_route.end_voice_turn_route", lambda *_a: None)
    monkeypatch.setattr("agent.turn_preflight_gate._clear_preflight_output_state", lambda *_a: None)
    return conversation_loop, seen


def test_run_conversation_reads_bound_session_origin(monkeypatch):
    from gateway.session_context import clear_session_vars, set_session_vars

    conversation_loop, seen = _capture_turn_origin(monkeypatch)
    tokens = set_session_vars(turn_origin="relay")
    try:
        conversation_loop.run_conversation(SimpleNamespace(), "hello")
    finally:
        clear_session_vars(tokens)
    assert seen["turn_origin"] == "relay"


def test_run_conversation_without_bound_origin_is_unknown(monkeypatch):
    from gateway.session_context import clear_session_vars, set_session_vars

    conversation_loop, seen = _capture_turn_origin(monkeypatch)
    tokens = set_session_vars()
    try:
        conversation_loop.run_conversation(SimpleNamespace(), "hello")
    finally:
        clear_session_vars(tokens)
    assert seen["turn_origin"] == "unknown"


def test_run_conversation_ignores_ambient_env_origin(monkeypatch):
    import contextvars

    from gateway.session_context import reset_session_vars

    conversation_loop, seen = _capture_turn_origin(monkeypatch)
    monkeypatch.setenv("HERMES_TURN_ORIGIN", "user")

    def run_unbound():
        reset_session_vars()
        conversation_loop.run_conversation(SimpleNamespace(), "hello")

    contextvars.copy_context().run(run_unbound)
    assert seen["turn_origin"] == "unknown"
