"""Kanban worker guidance must require owned task identity, not tools alone."""

from unittest.mock import patch


def _kanban_tool():
    return {"type": "function", "function": {"name": "kanban_show", "description": "show"}}


def _new_agent(monkeypatch):
    from run_agent import AIAgent

    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **_: [_kanban_tool()])
    return AIAgent(
        provider="custom",
        base_url="https://example.invalid/v1",
        api_key="sk-test",
        model="test-model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )


def test_init_requires_nonblank_owned_task_and_tool(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "  task-1  ")
    agent = _new_agent(monkeypatch)
    from agent.prompt_builder import KANBAN_GUIDANCE
    assert agent._kanban_worker_guidance == KANBAN_GUIDANCE


def test_init_empty_cache_is_stable_when_task_env_appears(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    agent = _new_agent(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    assert agent._kanban_worker_guidance == ""


def test_init_guidance_cache_is_stable_when_task_env_disappears(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    agent = _new_agent(monkeypatch)
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    from agent.prompt_builder import KANBAN_GUIDANCE
    assert agent._kanban_worker_guidance == KANBAN_GUIDANCE


def test_child_context_and_inherited_marker_do_not_get_guidance(monkeypatch):
    from agent.delegation_context import delegated_child_context

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    with delegated_child_context():
        agent = _new_agent(monkeypatch)
    assert agent._kanban_worker_guidance == ""

    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    agent = _new_agent(monkeypatch)
    assert agent._kanban_worker_guidance == ""


def test_cron_nonowned_context_does_not_get_guidance(monkeypatch):
    from agent.delegation_context import non_dispatcher_owned_context

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    with non_dispatcher_owned_context():
        agent = _new_agent(monkeypatch)
    assert agent._kanban_worker_guidance == ""


def test_prompt_fallback_uses_same_gate_and_preserves_cached_empty(monkeypatch):
    from agent.system_prompt import _tool_guidance_block
    from agent.prompt_builder import KANBAN_GUIDANCE
    from types import SimpleNamespace

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    with patch("agent.delegation_context.is_dispatcher_owned_worker_context", return_value=True):
        agent = SimpleNamespace(valid_tool_names={"kanban_show"}, _kanban_worker_guidance=None)
        assert _tool_guidance_block(agent) == KANBAN_GUIDANCE
        agent._kanban_worker_guidance = ""
        assert _tool_guidance_block(agent) is None

    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    agent = SimpleNamespace(valid_tool_names={"kanban_show"})
    assert _tool_guidance_block(agent) is None


def test_fallback_caches_empty_and_present_across_ambient_changes(monkeypatch):
    from agent.system_prompt import _tool_guidance_block
    from agent.prompt_builder import KANBAN_GUIDANCE
    from types import SimpleNamespace
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with patch("agent.delegation_context.is_dispatcher_owned_worker_context", return_value=True) as owned:
        empty = SimpleNamespace(valid_tool_names={"kanban_show"})
        assert _tool_guidance_block(empty) is None
        assert empty._kanban_worker_guidance == ""
        monkeypatch.setenv("HERMES_KANBAN_TASK", "task-new")
        assert _tool_guidance_block(empty) is None
        assert owned.call_count == 1
        active = SimpleNamespace(valid_tool_names={"kanban_show"})
        assert _tool_guidance_block(active) == KANBAN_GUIDANCE
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        owned.return_value = False
        assert _tool_guidance_block(active) == KANBAN_GUIDANCE
        assert owned.call_count == 2
