"""Goal stop outcomes must augment the normal reply, not bookkeeping notices."""

from __future__ import annotations

from unittest.mock import patch

from hermes_cli.goals import GoalManager


def test_prepare_goal_outcome_uses_semantic_judge_to_avoid_duplicate_explanation(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    manager = GoalManager("goal-outcome", default_max_turns=1)
    manager.set("Publish verified release notes")
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "release notes still need publication", False, None, False)):
        decision = manager.evaluate_after_turn("I drafted release notes.")

    current = (
        "The goal is paused before completion. I drafted release notes, but publication remains. "
        "The turn budget is exhausted; I need your approval to resume."
    )
    with patch("hermes_cli.goals._call_goal_judge_llm", return_value='{"already_explained": true, "supplement": ""}'):
        prepared = manager.prepare_goal_outcome(decision, current, tool_evidence=[])

    assert prepared == current
    goals._DB_CACHE.clear()


def test_prepare_goal_outcome_falls_back_to_truthful_stop_text_when_auxiliary_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    manager = GoalManager("goal-outcome-fallback", default_max_turns=1)
    manager.set("Publish verified release notes")
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "release notes still need publication", False, None, False)):
        decision = manager.evaluate_after_turn("I drafted release notes.")

    with patch("hermes_cli.goals._call_goal_judge_llm", side_effect=TimeoutError):
        prepared = manager.prepare_goal_outcome(decision, "I drafted release notes.", tool_evidence=[])

    assert prepared.startswith("I drafted release notes.")
    assert "Goal paused before completion." in prepared
    assert "turn budget exhausted" in prepared
    assert manager.state.status == "paused"
    goals._DB_CACHE.clear()
