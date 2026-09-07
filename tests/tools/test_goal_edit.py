"""Completion-contract editing preserves lifecycle and execution safeguards."""

import json

import pytest

from hermes_cli import goals
from tools.goal_authority import goal_user_request_scope
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
        goal="Parser regressions are fixed", **kwargs,
    ))


@pytest.mark.parametrize("status", ["active", "paused"])
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
    assert after["evidence_since"] > before["evidence_since"]
    for key in before.keys() - {"goal", "contract", "updated_at", "evidence_since"}:
        assert after[key] == before[key], key
    assert after["contract"] == {
        **before["contract"], "verification": "Parser and compatibility tests pass"
    }
    assert result["state"] == after


@pytest.mark.parametrize(
    "extra",
    [
        {"max_turns": 9},
        {"replace_existing": True},
        {"contract": {"verification": ""}},
        {"contract": {"verification": "   "}},
    ],
)
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


def test_edit_and_resume_is_one_persisted_state_without_stale_wait():
    """Update-and-continue changes objective and lifecycle in one write."""
    manager = goals.GoalManager("edit-test")
    old = manager.set("Fix parser")
    old.waiting_on_session = "old-process"
    old.waiting_on_delegation = "deleg_old"
    goals.save_goal("edit-test", old)

    result = json.loads(set_goal_tool(
        action="edit", session_id="edit-test", turn_id="turn",
        goal_control_revision=goals.get_goal_control_revision("edit-test"),
        goal="Ship the replacement", resume=True,
    ))
    assert result["success"] is True
    state = goals.load_goal("edit-test")
    persisted = goals.load_goal("edit-test")
    assert state is not None
    assert persisted is not None
    assert persisted.goal == "Ship the replacement"
    assert persisted.status == "active"
    assert persisted.waiting_on_session is None
    assert persisted.waiting_on_delegation is None
    assert state.to_json() == persisted.to_json()


def test_delegation_wait_is_typed_and_releases_without_judging(monkeypatch):
    manager = goals.GoalManager("edit-test")
    manager.set("Fix parser")
    record = {"parent_session_id": "edit-test", "origin_session_id": "", "state": "running"}
    monkeypatch.setattr(goals, "_delegation_dependency", lambda _: ("live", record))
    manager.wait_on_delegation("deleg_123", "parser subtask")
    decision = manager.evaluate_after_turn("verbose unsupported completion prose")
    assert decision["verdict"] == "waiting"
    assert manager.state.waiting_on_delegation == "deleg_123"
    monkeypatch.setattr(goals, "_delegation_dependency", lambda _: ("terminal", {**record, "state": "completed"}))
    assert manager.is_waiting() is False
    assert manager.state.waiting_on_delegation is None


def test_completed_goal_requires_atomic_resume_to_change_objective():
    state = goals.GoalManager("edit-test").set("Old objective")
    state.status = "done"
    goals.save_goal("edit-test", state)
    rejected = edit()
    assert rejected["success"] is False
    with goal_user_request_scope(
        "edit-test",
        "Set a goal to implement and land this, then resume QA and rerun to continue what you were doing before",
    ):
        resumed = edit(resume=True, user_requested=True)
    assert resumed["success"] is True
    assert resumed["state"]["status"] == "active"


def test_wait_rejects_ambiguous_or_out_of_scope_dependency_references():
    manager = goals.GoalManager("edit-test")
    manager.set("Parent")
    common = {
        "session_id": "edit-test", "turn_id": "turn",
        "goal_control_revision": goals.get_goal_control_revision("edit-test"),
    }
    ambiguous = json.loads(set_goal_tool(
        action="wait", pid=123, delegation_id="deleg_x", **common,
    ))
    assert ambiguous["success"] is False
    assert ambiguous["error_code"] == "ambiguous_dependency"
    misplaced = json.loads(set_goal_tool(
        action="status", delegation_id="deleg_x", **common,
    ))
    assert misplaced["success"] is False
    assert misplaced["error_code"] == "invalid_parameter"
    assert goals.load_goal("edit-test").waiting_on_delegation is None
