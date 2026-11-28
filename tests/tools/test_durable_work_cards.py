"""Durable work routes to Kanban cards in model-facing tool guidance (fork patch ``durable-work-cards``)."""

from tools.delegate_tool import _build_top_level_description
from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA


def _kanban_create_description() -> str:
    schema = KANBAN_CREATE_SCHEMA
    return schema.get("description") or schema["function"]["description"]


def test_delegate_guidance_sends_durable_work_to_a_task_keyed_card():
    for independent in (False, True):
        text = _build_top_level_description(independent_completions=independent)
        assert "kanban_create" in text
        assert '"task:<id>"' in text
        assert "goal_mode=true" in text
        assert "cronjob or terminal(background" not in text
        assert "a scheduled job -> cronjob" in text


def test_kanban_create_guidance_invites_any_session_to_start_durable_work():
    text = _kanban_create_description()
    assert "Any session" in text
    assert "instead of a background delegate" in text
    assert '"task:<id>"' in text
    assert "Done when:" in text and "Proof:" in text
    assert "Orchestrator workers also use it" in text


def test_tool_pin_is_keyed_by_the_running_commit(monkeypatch):
    """A deploy of new code must re-derive pinned tool definitions, so guidance changes reach live sessions."""
    import hermes_cli.version_info as version_info
    from tools import mcp_tool_agent

    monkeypatch.setattr(version_info, "get_code_identity", lambda refresh=False: {"sha": "a" * 40, "version": "0.0.0"})
    assert mcp_tool_agent.tool_pin_version() == "a" * 40
