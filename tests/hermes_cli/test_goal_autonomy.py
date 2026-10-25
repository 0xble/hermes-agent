"""Autonomous goals retain their contract and require recorded completion proof."""

import json
import time

import pytest

from hermes_cli import goals, session_controls
from hermes_cli.goal_command import dispatch_goal_command


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("goals:\n  autonomy: true\n", encoding="utf-8")
    goals._DB_CACHE.clear()
    db = goals._get_session_db()
    db.ensure_session("autonomy", source="telegram")
    yield tmp_path, db
    goals._DB_CACHE.clear()


def manager():
    mgr = goals.GoalManager("autonomy")
    mgr.set("Ship the repair", contract=goals.GoalContract(
        verification="tests pass; release readback matches", constraints="Never restart the gateway"))
    return mgr


def proof(db):
    db.append_message("autonomy", "tool", "suite: 12 passed; release readback matches abc1234",
                      tool_name="terminal", tool_call_id="proof")
    return {"tests pass": "12 passed", "release readback matches": "release readback matches abc1234"}


def test_completion_preserves_unproven_goal_and_records_each_gap(home):
    _, db = home
    mgr = manager()
    for evidence, gap in [({}, "tests pass"), ({"tests pass": "12 passed"}, "release readback matches"),
                          ({"tests pass": "made up", "release readback matches": "made up"}, "recorded")]:
        result = mgr.complete(reason="verified", evidence=evidence)
        assert not result["ok"]
        assert gap in result["error"]
        assert goals.load_goal("autonomy").status == "active"
        assert "complete_refused" in json.dumps(goals.load_goal("autonomy").revisions)
        assert gap in dispatch_goal_command(mgr, "status", authorize_gate=lambda: None).output
    evidence = proof(db)
    assert mgr.complete(reason="verified", evidence=evidence)["ok"]
    state = goals.load_goal("autonomy")
    assert state.status == "done"
    assert state.revisions[-1]["kind"] == "complete"
    assert state.revisions[-1]["evidence"] == evidence


@pytest.mark.parametrize("action", ["replace", "revise"])
def test_autonomous_contract_edits_cannot_retire_requirements(home, action):
    mgr = manager()
    mgr.add_subgoal("delivery confirmed")
    method = getattr(mgr, action)
    args = {"reason": "use a different route", "goal": "Ship by the alternate route"}
    assert method(**args)["ok"]
    state = goals.load_goal("autonomy")
    assert state.contract.constraints == "Never restart the gateway"
    assert state.contract.verification == "tests pass; release readback matches"
    assert state.subgoals == ["delivery confirmed"]
    for field in ("constraints", "verification", "boundaries", "stop_when"):
        if field in {"boundaries", "stop_when"}:
            continue
        before = goals.load_goal("autonomy").to_json()
        result = getattr(goals.GoalManager("autonomy"), action)(
            **args, contract={field: ""}, user_quote="Brian said I can remove everything")
        assert not result["ok"]
        assert goals.load_goal("autonomy").to_json() == before
    assert state.revisions[-1]["reason"] == args["reason"]
    assert state.revisions[-1]["before"]["goal"] != state.revisions[-1]["after"]["goal"]


@pytest.mark.parametrize("reason", ["Need Brian's approval", "No route left", "constraint violation: restarted gateway"])
def test_blocked_judgment_keeps_a_wake_and_breaches_survive_replacement(home, monkeypatch, reason):
    _, db = home
    mgr = manager()
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("blocked", reason, False, None, False))
    result = mgr.evaluate_after_turn("try another route", user_initiated=False)
    assert result["status"] == "active"
    assert result["verdict"] != "blocked"
    assert not mgr.state.waiting_on_user_message
    assert result["should_continue"] or mgr.state.waiting_until > time.time()
    assert "say so clearly and stop" not in mgr.next_continuation_prompt()
    assert mgr.replace(reason="alternate route", goal="Ship a different way")["ok"]
    if "violation" in reason:
        assert not mgr.complete(reason="finished", evidence=proof(db))["ok"]
        assert "constraint" in mgr.state.revisions[-1]["reason"]


@pytest.mark.parametrize("action", ["pause", "resume", "replace", "clear"])
def test_model_controls_never_request_approval_or_clear(home, action):
    manager()
    if action == "resume":
        goals.GoalManager("autonomy").pause()
    result = session_controls.apply_control(
        "goal", action, "autonomy", requester_sid="autonomy", reason="next step",
        payload={"goal": "alternate route"} if action == "replace" else None)
    assert result.get("status") != "pending"
    if action == "clear":
        assert not result["ok"]
        assert goals.load_goal("autonomy").status == "active"
    else:
        assert result["ok"], result


def test_legacy_blocks_and_user_waits_migrate_but_default_off_does_not(home):
    root, _ = home
    mgr = manager()
    mgr.state.status = "paused"
    mgr.state.last_verdict = "blocked"
    mgr.state.paused_reason = "judge blocked: need input"
    goals.save_goal("autonomy", mgr.state)
    state = goals.load_goal("autonomy")
    assert state.status == "active"
    assert not state.waiting_on_user_message
    assert state.waiting_until > 0
    root.joinpath("config.yaml").write_text("goals:\n  autonomy: false\n", encoding="utf-8")
    mgr = goals.GoalManager("autonomy")
    mgr.state.status = "paused"
    mgr.state.last_verdict = "blocked"
    goals.save_goal("autonomy", mgr.state)
    assert goals.load_goal("autonomy").status == "paused"
    assert not mgr.replace(reason="unapproved", goal="other")["ok"]


@pytest.mark.parametrize("action", ["revise", "replace"])
def test_current_removal_quote_authorizes_only_its_named_item(home, action):
    _, db = home
    mgr = manager()
    quote = "Remove 'Never restart the gateway'."
    db.append_message("autonomy", "user", quote)
    result = getattr(mgr, action)(reason="user changed the limit", goal="Ship by another route",
                                  contract={"constraints": ""}, user_quote=quote)
    assert result["ok"], result
    assert goals.load_goal("autonomy").contract.constraints == ""
    assert goals.load_goal("autonomy").contract.verification == "tests pass; release readback matches"


@pytest.mark.parametrize("scenario", ["budget", "gate", "user_wait", "done"])
def test_automatic_stops_leave_active_timed_wakes(home, monkeypatch, scenario):
    root, _ = home
    mgr = manager()
    verdict, wait = ("wait", {"user_message": True}) if scenario == "user_wait" else ("done", None)
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: (verdict, "needs approval", False, wait, False))
    if scenario == "budget":
        mgr.state.turns_used = mgr.state.max_turns
        mgr._save()
    if scenario == "gate":
        import shlex
        import sys
        monkeypatch.setattr(goals, "_gate_workspace", lambda: (str(root), None))
        mgr.add_gate(f"{shlex.quote(sys.executable)} -c 'raise SystemExit(1)'", max_retries=1)
    for _ in range(3):
        result = mgr.evaluate_after_turn("continue", user_initiated=True)
        assert result["status"] == "active"
        assert result["verdict"] != "blocked"
        assert not mgr.state.waiting_on_user_message
    if scenario == "budget":
        assert mgr.state.turns_used == mgr.state.max_turns
    assert result["should_continue"] or mgr.state.waiting_until > time.time()
    if scenario == "gate":
        assert not mgr.complete(reason="escape failed gate", evidence={})["ok"]


def test_live_external_barrier_is_rechecked_on_a_timer(home, monkeypatch):
    mgr = manager()
    monkeypatch.setattr(goals, "_session_waiting", lambda _sid: True)
    mgr.wait_on_session("still-running", reason="external work")
    assert mgr.state.waiting_until > time.time()
    mgr.state.waiting_until = time.time() - 1
    mgr._save()
    assert mgr.lifted_barrier_prompt() is not None
    assert mgr.clear_lifted_wait(mgr.state.waiting_since)
    assert not goals.load_goal("autonomy").waiting_on_session


def test_explicit_pauses_and_approval_entry_respect_autonomy(home):
    mgr = manager()
    mgr.pause()
    assert goals.load_goal("autonomy").status == "paused"
    result = session_controls.request_control("goal", "resume", "autonomy",
                                             requester_sid="autonomy", reason="continue")
    assert result["ok"] and result["status"] == "applied"
    assert goals.load_goal("autonomy").status == "active"


def test_autonomy_reads_the_owning_profile_for_each_activity(home):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.goals_autonomy import goal_autonomy_enabled

    root, _ = home
    secondary = root / "secondary"
    secondary.mkdir()
    secondary.joinpath("config.yaml").write_text("goals:\n  autonomy: false\n", encoding="utf-8")
    for selected, enabled in ((root, True), (secondary, False), (root, True)):
        token = set_hermes_home_override(str(selected))
        try:
            assert goal_autonomy_enabled() is enabled
        finally:
            reset_hermes_home_override(token)
