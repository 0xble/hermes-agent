"""CLI evaluates successful empty turns but never a failed-turn notice."""

import pytest

from hermes_cli import goals
from tests.hermes_cli.test_cli_goal_interrupt import _make_cli_with_goal

pytest_plugins = ["tests.hermes_cli.test_cli_goal_interrupt"]


@pytest.mark.parametrize("text", ["", "   ", "[SILENT]", "failure notice"])
@pytest.mark.parametrize("outcome", ["success", "failed", "interrupted", "incomplete"])
def test_goal_turn_outcome_controls_cli_judging(hermes_home, monkeypatch, text, outcome):
    cli, manager = _make_cli_with_goal("cli-empty-turn")
    cli._last_turn_result = {"final_response": text}
    cli._last_turn_result[{"success": "completed", "failed": "failed",
                           "interrupted": "interrupted", "incomplete": "completed"}[outcome]] = outcome != "incomplete"
    cli.conversation_history = [{"role": "assistant", "content": "an earlier response"}]
    calls = []
    def judge(*args, **kwargs):
        calls.append(args[1])
        return "done", "tool results prove completion", False, None, False
    monkeypatch.setattr(goals, "judge_goal", judge)
    cli._maybe_continue_goal_after_turn()
    assert calls == ([text] if outcome == "success" else [])
    assert manager.state.turns_used == int(outcome == "success")
    assert manager.state.status == ("done" if outcome == "success" else "active")
