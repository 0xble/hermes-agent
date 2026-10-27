"""A replacement changes only explicitly supplied goal definition fields."""
from __future__ import annotations

import pytest


@pytest.fixture
def goal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    manager = goals.GoalManager("preservation")
    manager.set("ALPHA", max_turns=12, contract=goals.GoalContract(
        verification="The canary passes", constraints="Keep the canary hard limit",
        boundaries="Canary scope only", stop_when="Ask before changing scope"))
    manager.add_subgoal("Retain the canary hard limit.")
    yield manager
    from hermes_state_registry import release_or_close
    for db in list(goals._DB_CACHE.values()):
        release_or_close(db)
    goals._DB_CACHE.clear()


@pytest.mark.parametrize("contract", [None, {}, {"verification": "BETA is verified"},
                                      {"boundaries": ""}])
def test_user_replacement_preserves_omitted_fields(goal, contract):
    from hermes_cli.goals import load_goal

    goal.state.turns_used = 5
    goal._save()
    before = load_goal("preservation")
    result = goal.replace(reason="User changed the outcome", goal="BETA", contract=contract, actor="user")
    assert result["ok"], result
    saved = load_goal("preservation")
    expected = before.contract.to_dict()
    expected.update(contract or {})
    assert saved.goal == "BETA"
    assert saved.contract.to_dict() == expected
    assert saved.subgoals == before.subgoals
    assert (saved.max_turns, saved.turns_used) == (before.max_turns, before.turns_used)
    assert saved.revisions[-1]["before"]["contract"] == before.contract.to_dict()
    assert saved.revisions[-1]["after"]["contract"] == expected


@pytest.mark.parametrize("status", ["active", "paused"])
@pytest.mark.parametrize("drafting", [False, True], ids=["inline", "draft"])
@pytest.mark.parametrize(("field", "label", "value"), [
    ("verification", "verify", "BETA is verified"),
    ("constraints", "constraints", "Keep the BETA hard limit"),
    ("boundaries", "boundaries", "BETA scope only"),
])
def test_bare_replacement_preserves_unmentioned_contract(
    goal, monkeypatch, status, drafting, field, label, value,
):
    from hermes_cli import goals
    from hermes_cli.goal_command import dispatch_goal_command

    goal.state.status = status
    goal.state.turns_used = 5
    goal._save()
    before = goals.load_goal("preservation")
    assert before is not None
    if drafting:
        monkeypatch.setattr(goals, "draft_contract", lambda _: goals.GoalContract(**{field: value}))
    command = "draft BETA" if drafting else f"BETA\n{label}: {value}"
    result = dispatch_goal_command(goal, command, authorize_gate=lambda: None)
    assert not result.error, result

    saved = goals.load_goal("preservation")
    assert saved is not None
    expected = before.contract.to_dict() | {field: value}
    assert saved.goal == "BETA"
    assert saved.contract.to_dict() == expected
    assert saved.subgoals == before.subgoals
    assert saved.status == before.status
    assert (saved.max_turns, saved.turns_used) == (before.max_turns, before.turns_used)
    assert saved.revisions[-1]["after"]["contract"] == expected
