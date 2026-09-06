"""Completion-contract editing preserves lifecycle and execution safeguards."""

import json

import pytest

from hermes_cli import goals
from tools.goal_tool import set_goal_tool


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    yield
    goals._DB_CACHE.clear()


def edit(**kwargs):
    return json.loads(set_goal_tool(
        action="edit", session_id="edit-test", turn_id="turn",
        goal_control_revision=goals.get_goal_control_revision("edit-test"),
        user_task="Refine the parser completion criteria.",
        authorization_text="Refine the parser completion criteria.",
        goal="Parser regressions are fixed", **kwargs,
    ))


@pytest.mark.parametrize("status", ["active", "paused", "done"])
def test_edit_preserves_lifecycle_and_unspecified_contract(status):
    manager = goals.GoalManager("edit-test")
    state = manager.set("Fix parser", max_turns=8, contract=goals.GoalContract(
        verification="Parser tests pass", constraints="Keep public API stable",
        boundaries="Parser only", stop_when="Blocked on user input",
    ))
    state.status = status
    state.turns_used = 5
    state.paused_reason = "user-paused" if status == "paused" else None
    state.subgoals = ["Cover empty input"]
    state.waiting_on_pid = 1234
    state.waiting_on_session = "process-test"
    state.waiting_until = 1234567890.0
    state.gates = [goals.GoalGate(command="python -m pytest tests/parser")]
    goals.save_goal("edit-test", state)
    before = json.loads(state.to_json())
    result = edit(contract={"verification": "Parser and compatibility tests pass"})
    assert result["success"] is True
    saved = goals.load_goal("edit-test")
    assert saved is not None
    after = json.loads(saved.to_json())
    for key in before.keys() - {"goal", "contract", "updated_at"}:
        assert after[key] == before[key], key
    assert after["contract"] == {**before["contract"], "verification": "Parser and compatibility tests pass"}
    assert result["state"] == after


@pytest.mark.parametrize("extra", [{"max_turns": 9}, {"replace_existing": True}, {"contract": {"verification": ""}}, {"contract": {"verification": "   "}}])
def test_edit_cannot_reset_budget_or_erase_verification(extra):
    manager = goals.GoalManager("edit-test")
    before = manager.set("Fix parser", contract=goals.GoalContract(verification="Tests pass"))
    result = edit(**extra)
    assert result["success"] is False
    saved = goals.load_goal("edit-test")
    assert saved is not None
    assert saved.to_json() == before.to_json()


def test_edit_without_goal_is_not_activation():
    assert edit()["error_code"] == "no_goal"
    assert goals.load_goal("edit-test") is None


def test_failed_edit_persistence_is_not_success(monkeypatch):
    before = goals.GoalManager("edit-test").set("Fix parser")
    monkeypatch.setattr(goals.GoalManager, "_persist_state", lambda *_: False)
    assert edit()["success"] is False
    saved = goals.load_goal("edit-test")
    assert saved is not None
    assert saved.to_json() == before.to_json()
