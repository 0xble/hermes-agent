"""A recoverable dependency must not be reported as an impossible goal."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def test_external_input_blocks_without_declaring_goal_unachievable(tmp_path, monkeypatch):
    from hermes_cli import goals

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    manager = goals.GoalManager("external-blocker")
    manager.set("Publish the verified patch")

    captured = {}

    def reply(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"verdict":"blocked","reason":"Waiting for the user to approve publication"}'
        ))])

    with patch("agent.auxiliary_client.call_llm", side_effect=reply):
        decision = manager.evaluate_after_turn("All local checks passed. Publication requires user approval.")

    assert decision["verdict"] == "blocked"
    assert decision["status"] == "paused"
    assert "unachievable" not in decision["message"].lower()
    assert "unachievable" not in manager.status_line().lower()
    assert "user input" in captured["prompt"].lower()
    assert "independent authorized work" in captured["prompt"].lower()
    paused = goals.load_goal("external-blocker")
    assert paused is not None and paused.status == "paused"
    assert goals.GoalManager("external-blocker").resume_for_user_input()
    resumed = goals.load_goal("external-blocker")
    assert resumed is not None and resumed.status == "active" and resumed.turns_used == 1
    goals._DB_CACHE.clear()
