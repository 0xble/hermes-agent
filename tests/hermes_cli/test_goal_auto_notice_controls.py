"""Goal auto-notice controls and grounded unexpected-stop explanations."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.inline_tool_executors import emit_terminal_post_tool_call
from hermes_cli.goals import GoalManager
from tools.goal_tool import set_goal_tool


def test_disabled_auto_notices_suppress_only_agent_goal_receipts(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    agent = SimpleNamespace(notice_callback=MagicMock(), _vprint=MagicMock())
    receipt = json.dumps({
        "success": True, "persisted": True, "change": {"goal": "Validate parser"},
        "notice": "Goal set: Validate parser",
    })

    with patch("hermes_cli.config.load_config", return_value={"goals": {"auto_notices": False}}):
        emit_terminal_post_tool_call(
            agent, function_name="set_goal", function_args={}, result=receipt,
            effective_task_id="task", tool_call_id="call",
        )

    assert json.loads(receipt)["persisted"] is True
    agent.notice_callback.assert_not_called()
    agent._vprint.assert_not_called()
    goals._DB_CACHE.clear()


def test_unexpected_budget_stop_has_grounded_explanation(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    manager = GoalManager("goal-stop", default_max_turns=1)
    manager.set("Validate parser compatibility")

    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "compatibility test remains", False, None, False)):
        decision = manager.evaluate_after_turn("I added the regression test.")

    explanation = decision["stop_explanation"]
    assert decision["status"] == "paused"
    assert decision["should_continue"] is False
    assert "Progress:" in explanation
    assert "1/1" in explanation
    assert "Unfinished:" in explanation
    assert "Validate parser compatibility" in explanation
    assert "Cause:" in explanation
    assert "compatibility test remains" in explanation
    assert "Next:" in explanation
    goals._DB_CACHE.clear()
