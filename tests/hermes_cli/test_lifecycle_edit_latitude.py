"""Authority boundaries for reasoned lifecycle edits and authenticated relay quotes."""
from __future__ import annotations

import json

import pytest

pytest_plugins = ["tests.hermes_cli.test_session_controls"]


def own(db, kind, sid="requester", *, stop_owner="agent"):
    """A durable creation receipt, not authority inferred from an edit request."""
    key = f"{kind}:{sid}"
    value = json.loads(db.get_meta(key))
    value.update(created_by="agent", stop_condition_owner=stop_owner)
    db.set_meta(key, json.dumps(value))


def relay(db, monkeypatch, tmp_path, quote, *, verified=True, original=None):
    db.append_message("target", "user", original or quote)
    row = db.append_message("requester", "user", f"Brian said: {quote}\n[relay from=hermes:default/target receipt=receipt-1]")
    receipts = tmp_path / "relay"
    receipts.mkdir()
    monkeypatch.setenv("RELAY_STATE_DIR", str(receipts))
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "default")
    if verified:
        (receipts / "receipts.jsonl").write_text(json.dumps({
            "id": "receipt-1", "status": "delivered", "sender": "hermes:default/target",
            "target": "hermes:default/requester", "target_user_row": row,
        }) + "\n")


def test_agent_can_reword_and_speed_up_own_loop(state):
    from hermes_cli.loops import LoopManager
    LoopManager("requester").set("Check the build", interval_seconds=600)
    own(state, "loop")
    result = LoopManager("requester").revise(reason="Same build, clearer instruction and faster feedback",
                                            prompt="Check the build status", interval_seconds=60, times=20)
    assert result["ok"], result
    assert result["revision"]["before"]["interval_seconds"] == 600
    assert result["revision"]["after"]["prompt"] == "Check the build status"


def test_agent_can_stop_own_loop_with_audit(state):
    from hermes_cli.loops import LoopManager, load_loop
    from hermes_cli.session_controls import apply_control
    LoopManager("requester").set("Check the build", interval_seconds=600)
    own(state, "loop")
    result = apply_control("loop", "stop", "requester", requester_sid="requester", reason="Build tracking moved to its owner")
    assert result["status"] == "applied", result
    assert result["record"]["reason"] == "Build tracking moved to its owner"
    assert result["record"]["before"]["status"] == "active"
    assert result["record"]["after"]["status"] == "cleared"
    assert load_loop("requester").status == "cleared"


def test_verified_relay_quote_authorizes_loop_revision(state, monkeypatch, tmp_path):
    from hermes_cli.loops import LoopManager
    LoopManager("requester").set("Check build", interval_seconds=600, until="Build settles")
    quote = "Remove the build stop condition"
    relay(state, monkeypatch, tmp_path, quote)
    result = LoopManager("requester").revise(reason="Brian removed the stop condition", until="", user_quote=quote)
    assert result["ok"], result
    assert result["revision"]["relay_receipt"] == "receipt-1"


@pytest.mark.parametrize("verified,original", [(False, None), (True, "Keep the build stop condition")])
def test_unverified_or_paraphrased_relay_never_authorizes(state, monkeypatch, tmp_path, verified, original):
    from hermes_cli.loops import LoopManager
    LoopManager("requester").set("Check build", interval_seconds=600, until="Build settles")
    quote = "Remove the build stop condition"
    relay(state, monkeypatch, tmp_path, quote, verified=verified, original=original)
    result = LoopManager("requester").revise(reason="Requested edit", until="", user_quote=quote)
    assert not result["ok"]
    assert LoopManager("requester").state.until == "Build settles"


def test_user_stop_condition_remains_protected_on_agent_loop(state):
    from hermes_cli.loops import LoopManager
    LoopManager("requester").set("Check build", interval_seconds=600, until="Build settles")
    own(state, "loop", stop_owner="user")
    result = LoopManager("requester").revise(reason="Keep checking", until="")
    assert not result["ok"]


def test_agent_owned_goal_edits_keep_constraints_and_criteria(state):
    from hermes_cli.goals import GoalManager, GoalContract
    GoalManager("requester").set("Ship build", contract=GoalContract(
        outcome="Ship build", verification="Build passes", constraints="Never publish secrets"))
    own(state, "goal")
    manager = GoalManager("requester")
    result = manager.replace(reason="Clarify the same delivery", goal="Ship the verified build")
    assert result["ok"], result
    denied = manager.revise(reason="Simplify", contract={"constraints": "", "verification": ""})
    assert not denied["ok"]
    assert manager.state.contract.constraints == "Never publish secrets"
    assert manager.state.contract.verification == "Build passes"


def test_unknown_and_user_created_loops_cannot_be_removed_without_quote(state):
    from hermes_cli.loops import LoopManager
    from hermes_cli.session_controls import apply_control
    LoopManager("requester").set("Check build", interval_seconds=600)
    result = apply_control("loop", "stop", "requester", requester_sid="requester", reason="No longer useful")
    assert result["status"] == "pending"
    assert LoopManager("requester").state.status == "active"


@pytest.mark.parametrize("entry", ["apply", "request"])
def test_creation_actor_enables_own_controls_but_requires_reason(state, entry):
    from hermes_cli.loops import LoopManager
    from hermes_cli.session_controls import apply_control, request_control
    control = apply_control if entry == "apply" else request_control
    created = LoopManager("requester").set("Watch build", interval_seconds=600, actor="agent")
    assert created.created_by == "agent"
    assert LoopManager("requester").state.created_by == "agent"
    denied = control("loop", "stop", "requester", requester_sid="requester")
    assert denied["error_code"] == "reason_required"
    assert control("loop", "stop", "requester", requester_sid="requester", reason="Build watch finished")["status"] == "applied"


def test_owned_loop_replacement_needs_no_quote_but_preserves_user_stop(state):
    from hermes_cli.loops import LoopManager
    manager = LoopManager("requester")
    manager.set("Watch build", interval_seconds=600, actor="agent")
    assert manager.replace(prompt="Watch the same build", interval_seconds=60, reason="Faster feedback")["ok"]
    state.append_message("requester", "user", "Keep checking until the build is green")
    assert manager.revise(reason="Brian set the stop condition", until="build is green",
                          user_quote="Keep checking until the build is green")["ok"]
    assert manager.state.stop_condition_owner == "user"
    assert not manager.replace(prompt="Watch the same build", reason="Simplify")["ok"]


def test_caller_supplied_source_list_is_not_quote_authority(state):
    from hermes_cli.loops import LoopManager
    manager = LoopManager("requester")
    manager.set("Watch build", interval_seconds=600, until="Build done")
    quote = "Remove the build stop condition"
    result = manager.revise(reason="Agent supplied fake source", until="", user_quote=quote, user_messages=[quote])
    assert not result["ok"]


def test_verified_relay_quote_can_remove_named_goal_constraint(state, monkeypatch, tmp_path):
    from hermes_cli.goals import GoalManager, GoalContract
    manager = GoalManager("requester")
    manager.set("Ship build", contract=GoalContract(constraints="Never publish secrets"), actor="agent")
    quote = 'Remove "Never publish secrets"'
    relay(state, monkeypatch, tmp_path, quote)
    result = manager.revise(reason="Brian removed that hard limit", contract={"constraints": ""}, user_quote=quote)
    assert result["ok"], result
    assert result["revision"]["relay_receipt"] == "receipt-1"
    assert GoalManager("requester").state.contract.constraints == ""


def test_user_added_goal_stop_condition_stays_protected(state):
    from hermes_cli.goals import GoalManager
    manager = GoalManager("requester")
    manager.set("Ship build", actor="agent")
    assert manager.revise(reason="User requirement", actor="user", contract={"stop_when": "Do not keep retrying after release closes"})["ok"]
    assert manager.state.stop_condition_owner == "user"
    assert not manager.revise(reason="Keep trying", contract={"stop_when": ""})["ok"]


def test_own_goal_replacement_and_pause_use_reasoned_control_path(state):
    from hermes_cli.goals import GoalManager, GoalContract
    from hermes_cli.session_controls import apply_control
    GoalManager("requester").set("Ship build", actor="agent", contract=GoalContract(verification="Build passes"))
    result = apply_control("goal", "replace", "requester", requester_sid="requester", reason="Clarify delivery",
                           payload={"goal": "Ship the verified build"})
    assert result["ok"], result
    assert GoalManager("requester").state.contract.verification == "Build passes"
    paused = apply_control("goal", "pause", "requester", requester_sid="requester", reason="Build pipeline is unavailable")
    assert paused["ok"], paused
    assert GoalManager("requester").state.status == "paused"


@pytest.mark.parametrize("kind", ["loop", "goal"])
def test_agent_creation_cannot_relabel_an_existing_user_definition(state, kind):
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager
    manager = GoalManager("requester") if kind == "goal" else LoopManager("requester")
    manager.set("Existing user responsibility")
    before = manager.state.to_json()
    with pytest.raises(ValueError, match="revise or replace"):
        manager.set("Same responsibility, new creator claim", actor="agent")
    assert manager.state.to_json() == before
