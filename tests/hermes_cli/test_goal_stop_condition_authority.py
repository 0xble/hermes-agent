"""A contextual user removal is not authority to discard the rest of a goal."""
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
    mgr.set("Ship the incentive method", contract=GoalContract(
        outcome="The method works", verification="Live calculation passes",
        constraints="Do not change compensation", boundaries="The incentive system",
        stop_when="scope must change beyond incentive method",
    ))
    mgr.add_subgoal("Keep the payout ceiling")
    yield db, mgr
    goals._DB_CACHE.clear()
    release_or_close(db)


def _replace(mgr, **changes):
    contract = mgr.state.contract.to_dict()
    contract["stop_when"] = ""
    payload = dict(goal=mgr.state.goal, contract=contract)
    for field, value in changes.items():
        if field in contract:
            contract[field] = value
        else:
            payload[field] = value
    return sc.apply_control("goal", "replace", "work", requester_sid="work",
                            user_quote="Remove what says this", payload=payload,
                            reason="User removed the stop condition")


@pytest.mark.parametrize("stop_when", ["", "Stop only if the requested outcome changes"])
def test_fresh_contextual_stop_edit_keeps_the_goal_and_user_requirements(goal_session, stop_when):
    db, mgr = goal_session
    db.append_message("work", "user", "Remove what says this")
    before = json.loads(mgr.state.to_json())

    result = _replace(mgr, stop_when=stop_when)

    assert result.get("status") == "applied", result
    after = GoalManager("work").state
    assert after.goal == before["goal"]
    assert after.subgoals == before["subgoals"]
    assert after.contract.stop_when == stop_when
    for field in ("outcome", "verification", "constraints", "boundaries"):
        assert getattr(after.contract, field) == before["contract"][field]
    assert after.revisions[-1]["authority"] == "quote"
    assert after.revisions[-1]["user_quote"] == "Remove what says this"
    assert all(r["status"] != "pending" for r in sc.pending_outbox())


@pytest.mark.parametrize("changes, message", [
    ({"goal": "Ship something else"}, "Remove what says this"),
    ({"verification": ""}, "Remove what says this"),
    ({"outcome": ""}, "Remove what says this"),
    ({"constraints": ""}, "Remove what says this"),
    ({"boundaries": ""}, "Remove what says this"),
    ({"max_turns": 1}, "Remove what says this"),
    ({}, "Do not Remove what says this"),
    ({}, "Remove what says this?"),
    ({}, "If the checks pass, Remove what says this"),
    ({}, 'The log says "Remove what says this"'),
    ({}, "That earlier removal is no longer current"),
])
def test_contextual_stop_removal_does_not_authorize_dropping_other_requirements(goal_session, changes, message):
    db, mgr = goal_session
    db.append_message("work", "user", "Remove what says this")
    if message != "Remove what says this":
        db.append_message("work", "user", message)
    before = json.loads(mgr.state.to_json())

    result = _replace(mgr, **changes)

    assert result.get("status") != "applied", result
    assert json.loads(GoalManager("work").state.to_json()) == before
