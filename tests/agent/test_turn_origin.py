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
