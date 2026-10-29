"""Stop-condition authority binds the instruction and delta, not an unrelated imperative."""
from __future__ import annotations

import json

import pytest

from hermes_cli import session_controls as sc
from hermes_cli.goals import GoalContract, GoalManager


@pytest.fixture
def goal_session(tmp_path, monkeypatch):
    from hermes_cli import goals
    from hermes_state_registry import acquire, release_or_close

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    db = acquire(home / "state.db")
    goals._DB_CACHE[str(home)] = db
    db.create_session("work", "telegram", profile_name="default", chat_id="work",
                      chat_type="private", session_key="agent:main:telegram:dm:work")
    mgr = GoalManager("work")
    mgr.set("Ship the incentive method", max_turns=20, contract=GoalContract(
        outcome="The method works", verification="Live calculation passes",
        constraints="Do not change compensation", boundaries="The incentive system",
        stop_when="scope must change beyond incentive method",
    ))
    mgr.add_subgoal("Keep the payout ceiling")
    yield db, mgr
    goals._DB_CACHE.clear()
    release_or_close(db)


def _replace(mgr, user_quote="Clear the stop condition", **changes):
    contract = mgr.state.contract.to_dict()
    contract["stop_when"] = ""
    payload = dict(goal=mgr.state.goal, contract=contract)
    for field, value in changes.items():
        if field in contract:
            contract[field] = value
        else:
            payload[field] = value
    return sc.apply_control("goal", "replace", "work", requester_sid="work",
                            user_quote=user_quote, payload=payload,
                            reason="User removed the stop condition")


@pytest.mark.parametrize("message", [
    "Clear the stop condition",
    "Please remove this stop condition now",
    "Delete the stop condition",
    "Drop the stop condition",
])
def test_explicit_stop_removal_keeps_the_goal_and_user_requirements(goal_session, message):
    db, mgr = goal_session
    db.append_message("work", "user", message)
    before = json.loads(mgr.state.to_json())

    result = _replace(mgr, user_quote=message)

    assert result.get("status") == "applied", result
    after = GoalManager("work").state
    assert after is not None
    assert after.goal == before["goal"]
    assert after.subgoals == before["subgoals"]
    assert after.contract.stop_when == ""
    assert after.max_turns == before["max_turns"]
    assert after.turns_used == before["turns_used"]
    for field in ("outcome", "verification", "constraints", "boundaries"):
        assert getattr(after.contract, field) == before["contract"][field]
    assert after.revisions[-1]["authority"] == "quote"
    assert after.revisions[-1]["user_quote"] == message
    assert all(r["status"] != "pending" for r in sc.pending_outbox())


@pytest.mark.parametrize("changes, message, source", [
    ({"goal": "Ship something else"}, "Clear the stop condition", None),
    ({"verification": ""}, "Clear the stop condition", None),
    ({"outcome": ""}, "Clear the stop condition", None),
    ({"constraints": ""}, "Clear the stop condition", None),
    ({"boundaries": ""}, "Clear the stop condition", None),
    ({"max_turns": 1}, "Clear the stop condition", None),
    ({"stop_when": "never stop"}, "Clear the stop condition", None),
    ({}, "Delete that branch from the remote", None),
    ({"stop_when": "never stop"}, "Update this file with the new numbers", None),
    ({"stop_when": "never stop"}, "Change it to blue", None),
    ({}, "Remove what says this", None),
    ({}, "Clear the stop condition from this file", None),
    ({}, "Delete this branch with the stop condition", None),
    ({}, "Remove the loop stop condition", None),
    ({}, "Clear the stop condition", "Clear the stop condition?"),
    ({}, "Clear the stop condition", 'The log says "Clear the stop condition"'),
    ({}, "Clear the stop condition", "That earlier removal is no longer current"),
    ({}, "Remove what says this", '[Replying to: "The stop condition blocks progress"]\nRemove what says this'),
])
def test_unbound_or_broader_stop_edits_require_approval(goal_session, changes, message, source):
    db, mgr = goal_session
    db.append_message("work", "user", message)
    if source is not None:
        db.append_message("work", "user", source)
    before = json.loads(mgr.state.to_json())

    result = _replace(mgr, user_quote=message, **changes)

    assert result.get("status") == "pending", result
    assert json.loads(GoalManager("work").state.to_json()) == before


def test_underspecified_replacement_does_not_grant_quote_authority(goal_session):
    db, mgr = goal_session
    message = "Replace the goal with what we discussed"
    db.append_message("work", "user", message)
    before = json.loads(mgr.state.to_json())

    result = _replace(mgr, user_quote=message, goal="An agent-authored objective")

    assert result.get("status") == "pending", result
    assert result.get("quote_refused") == "user_quote_qualified"
    after = GoalManager("work").state
    assert after is not None
    assert json.loads(after.to_json()) == before


@pytest.mark.parametrize("source, quote, refusal", [
    ("Do not clear the stop condition", "clear the stop condition", "user_quote_negated"),
    ("Clear the stop condition if the checks pass", "Clear the stop condition", "user_quote_qualified"),
])
def test_stop_removal_checks_the_exact_quote_context(goal_session, source, quote, refusal):
    db, mgr = goal_session
    db.append_message("work", "user", source)
    before = json.loads(mgr.state.to_json())

    result = _replace(mgr, user_quote=quote)

    assert result.get("status") == "pending", result
    assert result.get("quote_refused") == refusal
    after = GoalManager("work").state
    assert after is not None
    assert json.loads(after.to_json()) == before


@pytest.mark.parametrize("stop_when", ["", "A Hermes restart is needed"])
def test_button_approved_stop_only_edit_preserves_subgoals(goal_session, stop_when):
    _, mgr = goal_session
    before = json.loads(mgr.state.to_json())
    pending = _replace(mgr, user_quote="", stop_when=stop_when)
    assert pending.get("status") == "pending", pending

    result = sc.resolve_request(pending["request_id"], "approve", "admin")

    assert result is not None and result.get("status") == "applied", result
    after = GoalManager("work").state
    assert after is not None
    assert after.subgoals == before["subgoals"]
    assert after.contract.stop_when == stop_when
    assert after.max_turns == before["max_turns"]
    assert after.revisions[-1]["authority"] == "button"
