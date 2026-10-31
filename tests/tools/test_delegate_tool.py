"""Incognito delegation must remain synchronous and leave no child side channels."""
import json
from unittest.mock import MagicMock, patch

from agent.incognito import incognito_scope
from tests.tools.test_delegate import _make_mock_parent
from tools.delegate_tool import _build_child_agent, delegate_task


def test_private_child_has_no_database_and_inherits_flag():
    parent = _make_mock_parent()
    parent.incognito = True
    parent.session_id = "private-parent"
    with patch("run_agent.AIAgent") as agent, patch("tools.delegate_tool._open_child_session_db") as database:
        agent.return_value = MagicMock()
        _build_child_agent(task_index=0, goal="private goal", context=None, toolsets=None,
                           model=None, max_iterations=4, parent_agent=parent, task_count=1, role="leaf")
    assert agent.call_args.kwargs["incognito"] is True
    assert agent.call_args.kwargs["session_db"] is None
    database.assert_not_called()


def test_default_child_constructor_preserves_off_contract():
    parent = _make_mock_parent()
    parent.incognito = False
    with patch("run_agent.AIAgent") as agent:
        _build_child_agent(task_index=0, goal="normal goal", context=None, toolsets=None,
                           model=None, max_iterations=4, parent_agent=parent, task_count=1, role="leaf")
    assert "incognito" not in agent.call_args.kwargs


def test_private_async_refuses_before_credentials_or_disk():
    parent = _make_mock_parent()
    parent.incognito = True
    with patch("tools.delegate_tool._resolve_delegation_credentials") as credentials:
        result = json.loads(delegate_task(goal="do not persist", parent_agent=parent, background=True))
    assert "incognito" in result["error"].lower()
    credentials.assert_not_called()


def test_private_delegation_still_joins_children_without_live_cache():
    parent = _make_mock_parent()
    parent.incognito = True
    parent._oneshot_spawned_children = 0
    parent.session_id = "private-parent-sync"
    parent._delegate_spawn_limit = 100
    child = MagicMock()
    child.session_id = "private-child"
    child.run_conversation.return_value = {"final_response": "private answer", "messages": []}
    child._interrupt_requested = False
    with (
        patch("tools.delegate_tool._resolve_delegation_credentials", return_value={
            "model": "test", "provider": "openai", "base_url": "https://example.invalid/v1",
            "api_key": "test", "api_mode": "chat_completions",
        }),
        patch("tools.delegate_tool._build_child_agent", return_value=child),
        patch("tools.delegation_live_log.create_live_transcripts") as live,
        patch("tools.delegate_tool_dispatch.begin_delegation_run") as metrics,
        patch("tools.delegate_tool_results._spill_summary_to_file") as cache,
    ):
        result = json.loads(delegate_task(goal="private goal", parent_agent=parent))
    assert result["results"][0]["summary"] == "private answer"
    child.run_conversation.assert_called_once()
    live.assert_not_called()
    metrics.assert_not_called()
    cache.assert_not_called()


def test_direct_cache_sinks_refuse_without_creating_directories(tmp_path, monkeypatch):
    from tools.delegate_tool_results import _spill_summary_to_file
    from tools.delegation_live_log import create_live_transcripts
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    before = set(tmp_path.rglob("*"))
    with incognito_scope():
        assert _spill_summary_to_file(0, "private-output") is None
        assert create_live_transcripts([{"goal": "private"}], "private context") == (None, [None], [])
    assert set(tmp_path.rglob("*")) == before
