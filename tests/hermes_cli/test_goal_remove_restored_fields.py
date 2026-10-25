"""Every migrated contract field can be explicitly retired through shared /goal dispatch."""

import json

import pytest

from hermes_cli import goals
from hermes_cli.goal_command import dispatch_goal_command
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    store = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: store)
    yield store
    store.close()


def _legacy_manager(db):
    before = {
        "goal": "Ship X", "verification": "Audit passes", "constraints": "Keep records",
        "boundaries": "Stay in staging", "stop_when": "Stop without credentials",
        "subgoals": ["Smoke test both services"],
    }
    after = {**dict.fromkeys(before, ""), "goal": "Ship Y", "subgoals": []}
    legacy = goals.GoalState(
        goal="Ship Y", status="active", max_turns=7, turns_used=2,
        revisions=[{"actor": "agent", "before": before, "after": after}],
    )
    db.set_meta("goal:legacy", legacy.to_json())
    return goals.GoalManager("legacy")


def _dispatch(manager, arg):
    return dispatch_goal_command(manager, arg, authorize_gate=lambda: None)


@pytest.mark.parametrize("kind,field,item", [
    ("outcome", "outcome", "Ship X"),
    ("check", "verification", "Audit passes"),
    ("verification", "verification", "Audit passes"),
    ("constraint", "constraints", "Keep records"),
    ("constraints", "constraints", "Keep records"),
    ("boundary", "boundaries", "Stay in staging"),
    ("boundaries", "boundaries", "Stay in staging"),
    ("stop", "stop_when", "Stop without credentials"),
    ("stop_when", "stop_when", "Stop without credentials"),
    ("subgoal", "subgoals", "Smoke test both services"),
])
def test_explicit_command_removes_restored_field_and_persists_without_rebinding(db, kind, field, item):
    manager = _legacy_manager(db)
    state = goals.load_goal("legacy")
    assert state is not None
    values = state.subgoals if field == "subgoals" else getattr(state.contract, field)
    assert goals.contract_items(field, values) == [item]
    assert state.revisions[0]["legacy_binding_migrated"] is True
    # Replacement edits only the objective, not unrelated or restored contract items.
    replacement = _dispatch(manager, "replace Ship Y")
    assert not replacement.error and item in replacement.prompt

    result = _dispatch(manager, f"remove {kind} 1")

    assert not result.error, result.output
    assert result.output == f"✓ Removed {kind} 1: {item}"
    assert result.clear_pending == "remove"
    assert result.prompt is not None and item not in result.prompt
    loaded = goals.load_goal("legacy")
    assert loaded is not None
    values = loaded.subgoals if field == "subgoals" else getattr(loaded.contract, field)
    assert goals.contract_items(field, values) == []
    assert item not in loaded.contract.render_block()
    assert item not in goals.GoalManager("legacy").next_continuation_prompt()
    assert loaded.goal == "Ship Y" and loaded.status == "active"
    assert (loaded.max_turns, loaded.turns_used) == (7, 2)
    removal = json.loads(db.get_meta("goal:legacy"))["revisions"][-1]
    assert removal["kind"] == "remove" and removal["authority"] == "explicit_command"
    assert removal["before"] == {field: [item]} and removal["after"] == {field: []}


def test_status_and_show_number_every_removable_field_and_invalid_kind_still_fails(db):
    manager = _legacy_manager(db)
    for command in ("status", "show"):
        result = _dispatch(manager, command)
        assert not result.error
        for label, item in [
            ("Outcome", "Ship X"), ("Verification", "Audit passes"),
            ("Constraints", "Keep records"), ("Boundaries", "Stay in staging"),
            ("Stop when blocked", "Stop without credentials"),
            ("Subgoals", "Smoke test both services"),
        ]:
            assert f"{label}:\n- 1. {item}" in result.output
    stored = db.get_meta("goal:legacy")

    invalid = _dispatch(manager, "remove imaginary 1")

    assert invalid.error and invalid.output.startswith("Usage: /goal remove <")
    assert invalid.prompt is None and invalid.clear_pending is None
    for kind in ("outcome", "check", "constraint", "boundary", "stop", "subgoal"):
        assert kind in invalid.output
    assert db.get_meta("goal:legacy") == stored


def test_separator_only_fragments_never_become_numbered_items():
    from hermes_cli.goals import contract_items
    assert contract_items("constraints", "No downtime. ; No secrets.") == ["No downtime.", "No secrets."]
    assert contract_items("constraints", "A; ;\n;\nB.") == ["A", "B."]


def test_continuation_routes_retirement_to_user_commands_not_evidence_revisions():
    from hermes_cli.goals import CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE as template
    assert "revise a verification item through goal_set" not in template
    assert "can only add requirements" in template and "/goal remove" in template
