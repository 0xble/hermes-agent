"""Autonomous goals retain their contract and require recorded completion proof."""

import json
import time

import pytest

# User-message persistence reaches the runtime bootstrap. Load it before the
# per-test real-home guard, as the application entry point does.
import hermes_bootstrap  # noqa: F401

from hermes_cli import goals, session_controls
from hermes_cli.goal_command import dispatch_goal_command


@pytest.fixture
def home(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("goals:\n  autonomy: true\n", encoding="utf-8")
    goals._DB_CACHE.clear()
    db = goals._get_session_db()
    db.create_session("autonomy", "telegram", profile_name="default", chat_id="autonomy",
                      chat_type="private", session_key="agent:main:telegram:dm:autonomy")
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


@pytest.mark.parametrize("excerpt", ["ok", "successfully", "all checks passed", "done done done"])
def test_completion_refuses_generic_recorded_excerpts(home, excerpt):
    _, db = home
    mgr = manager()
    db.append_message("autonomy", "tool", excerpt, tool_name="terminal")
    result = mgr.complete(reason="finished", evidence={item: excerpt for item in (
        "tests pass", "release readback matches")})
    assert not result["ok"] and "tests pass" in result["error"]
    assert "concrete recorded proof" in result["error"]
    assert goals.load_goal("autonomy").status == "active"


@pytest.mark.parametrize("status", ["cleared", "done"])
@pytest.mark.parametrize("barrier", ["user", "pid", "session"])
def test_autonomy_migration_never_revives_terminal_goals(home, status, barrier):
    _, db = home
    mgr = manager()
    mgr.state.status = status
    if barrier == "user":
        mgr.state.waiting_on_user_message = True
    elif barrier == "pid":
        mgr.state.waiting_on_pid = 123
    else:
        mgr.state.waiting_on_session = "other"
    goals.save_goal("autonomy", mgr.state)
    expected = db.get_meta("goal:autonomy")
    assert goals.load_goal("autonomy").status == status
    assert goals.list_parked_goals() == []
    assert not goals.store_has_parked_goal(db)
    assert db.get_meta("goal:autonomy") == expected


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
    elif action == "resume":
        # The pause above is the user's, which the agent never lifts.
        assert not result["ok"] and result["error_code"] == "user_paused", result
        assert goals.load_goal("autonomy").status == "paused"
    else:
        assert result["ok"], result


@pytest.mark.parametrize("pause", ["slash", "stop", "quoted_resume", "agent"])
def test_agent_resumes_only_its_own_goal_pause(home, pause):
    _, db = home
    mgr = manager()
    if pause == "slash":
        dispatch_goal_command(mgr, "pause", authorize_gate=lambda: None)
    elif pause == "stop":
        goals.GoalManager("autonomy").pause(reason="user-interrupted (/stop)")
    elif pause == "quoted_resume":
        dispatch_goal_command(mgr, "pause", authorize_gate=lambda: None)
        db.append_message("autonomy", "user", "Resume the goal.")
    else:
        goals.GoalManager("autonomy").pause(reason="waiting on a deploy", by="agent")
    quote = "Resume the goal." if pause == "quoted_resume" else ""
    result = session_controls.apply_control("goal", "resume", "autonomy", requester_sid="autonomy",
                                            reason="next step", user_quote=quote)
    state = goals.load_goal("autonomy")
    if pause in {"slash", "stop"}:
        assert not result["ok"] and result["error_code"] == "user_paused", result
        assert state.status == "paused" and state.paused_by == "user"
    else:
        assert result["ok"], result
        assert state.status == "active" and state.paused_by == ""


@pytest.mark.parametrize("entry", ["apply", "request"])
@pytest.mark.parametrize("reason,user_quote", [
    ("temporary dependency wait", ""),
    ("user-paused", ""),
    ("session-control", "Pause the goal."),
    ("next step", "Continue the goal."),
])
def test_model_pause_has_a_timed_native_wake(home, monkeypatch, entry, reason, user_quote):
    _, db = home
    mgr = manager()
    mgr.add_subgoal("delivery confirmed")
    db.append_message("autonomy", "user", "Continue the goal.")
    before = goals.load_goal("autonomy")
    control = session_controls.apply_control if entry == "apply" else session_controls.request_control
    kwargs = {"user_quote": user_quote} if entry == "apply" else {}
    result = control("goal", "pause", "autonomy", requester_sid="autonomy", reason=reason, **kwargs)
    assert result["ok"] and result["status"] == "applied", result
    state = goals.load_goal("autonomy")
    assert state.status == "active"
    assert state.waiting_until > time.time()
    assert not state.waiting_on_user_message and state.paused_reason is None
    assert state.contract == before.contract and state.subgoals == before.subgoals
    assert (state.turns_used, state.max_turns, state.created_at) == (
        before.turns_used, before.max_turns, before.created_at)
    assert goals.store_has_parked_goal(db)
    assert any(sid == "autonomy" for sid, _ in goals.list_parked_goals())
    wake = goals.GoalManager("autonomy")
    assert wake.lifted_barrier_prompt() is None
    monkeypatch.setattr(goals.time, "time", lambda: state.waiting_until + 1)
    assert wake.lifted_barrier_prompt() is not None
    assert wake.clear_lifted_wait(state.waiting_since)
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "next route", False, None, False))
    decision = wake.evaluate_after_turn("try the next route", user_initiated=False)
    assert decision["status"] == "active" and wake.state.turns_used == before.turns_used + 1


@pytest.mark.parametrize("entry", ["slash", "quote", "stale_quote", "default_off"])
def test_user_pause_authority_stays_explicit(home, monkeypatch, entry):
    root, db = home
    mgr = manager()
    quote = "Pause the goal."
    db.append_message("autonomy", "user", quote)
    if entry == "slash":
        dispatch_goal_command(mgr, "pause", authorize_gate=lambda: None)
    else:
        if entry == "default_off":
            root.joinpath("config.yaml").write_text("goals:\n  autonomy: false\n", encoding="utf-8")
        if entry == "stale_quote":
            check = session_controls.check_user_quote

            def supersede(sid, text, *, cursor=None):
                checked = check(sid, text, cursor=cursor)
                if cursor is None:
                    db.append_message("autonomy", "user", "Continue the goal.")
                return checked

            monkeypatch.setattr(session_controls, "check_user_quote", supersede)
        result = session_controls.apply_control("goal", "pause", "autonomy",
            requester_sid="autonomy", reason="temporary dependency wait", user_quote=quote)
        assert result["ok"] and result["status"] == "applied", result
    state = goals.load_goal("autonomy")
    if entry == "stale_quote":
        assert state.status == "active" and state.waiting_until > time.time()
    else:
        assert state.status == "paused" and not state.waiting_until
        assert not goals.store_has_parked_goal(db)
        assert goals.list_parked_goals() == []
        assert goals.GoalManager("autonomy").lifted_barrier_prompt() is None


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
        mgr.state.turns_used = mgr.state.max_turns - 1
        mgr._save()
    if scenario == "gate":
        import shlex
        import sys
        monkeypatch.setattr(goals, "_gate_workspace", lambda: (str(root), None))
        mgr.add_gate(f"{shlex.quote(sys.executable)} -c 'raise SystemExit(1)'", max_retries=1)
    for _ in range(1 if scenario == "budget" else 3):
        result = mgr.evaluate_after_turn("continue", user_initiated=True)
        assert result["status"] == "active"
        assert result["verdict"] != "blocked"
        assert not mgr.state.waiting_on_user_message
        assert "say so clearly and stop" not in (result.get("continuation_prompt") or "")
    if scenario == "budget":
        assert mgr.state.turns_used == 0
        assert mgr.state.revisions[-1]["kind"] == "budget_checkpoint"
        assert mgr.state.revisions[-1]["turns_used"] == mgr.state.max_turns
        mgr.state.waiting_until = time.time() - 1
        mgr._save()
        result = mgr.evaluate_after_turn("next route", user_initiated=False)
        assert mgr.state.turns_used == 1 and mgr.state.status == "active"
    assert result["should_continue"] or mgr.state.waiting_until > time.time()
    if scenario == "gate":
        assert not mgr.complete(reason="escape failed gate", evidence={})["ok"]


@pytest.mark.parametrize("gap", [0, 1800])
def test_live_external_barrier_is_rechecked_on_a_timer(home, monkeypatch, gap):
    manager()
    mgr = goals.GoalManager("autonomy", min_continuation_gap_seconds=gap)
    monkeypatch.setattr(goals, "_session_waiting", lambda _sid: True)
    mgr.wait_on_session("still-running", reason="external work")
    assert mgr.state.waiting_until > time.time()
    assert mgr.state.waiting_until - mgr.state.waiting_since >= max(900, gap) - 1
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
    # Autonomy answers at once with no approval card, but an explicit user pause stands.
    assert result.get("status") != "pending"
    assert not result["ok"] and result["error_code"] == "user_paused", result
    assert goals.load_goal("autonomy").status == "paused"


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


def test_prompt_generation_retains_unreachable_shipped_wordings(home, monkeypatch):
    from scripts.dev import snapshot_goal_prompt_history as generator

    root, _ = home
    registry = root / "registry.py"
    shipped = {"formatter": "plain", "template": "old shipped prompt {goal}", "source_commits": ("retired",)}
    registry.write_text("\ufeffGOAL_PROMPT_REGISTRY: tuple = " + repr((shipped,)), encoding="utf-8")
    monkeypatch.setattr(generator, "OUTPUT", registry)
    monkeypatch.setattr(generator, "SOURCE_FILES", ())
    monkeypatch.setattr(generator, "_commits", list)
    assert generator._snapshot()["plain"][shipped["template"]] == ["retired"]


@pytest.mark.parametrize("reason", ["Need approval to avoid violating a constraint", "No constraint violation occurred"])
def test_a_prevented_breach_does_not_invalidate_later_completion(home, monkeypatch, reason):
    _, db = home
    mgr = manager()
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("blocked", reason, False, None, False))
    mgr.evaluate_after_turn("try another route")
    assert mgr.complete(reason="verified without a breach", evidence=proof(db))["ok"]


def test_completion_cannot_overwrite_a_new_requirement(home, monkeypatch):
    _, db = home
    mgr = manager()
    evidence = proof(db)
    original = mgr._recorded_evidence_error

    def interleave(excerpt, state):
        goals.GoalManager("autonomy").add_subgoal("New delivery readback")
        return original(excerpt, state)

    monkeypatch.setattr(mgr, "_recorded_evidence_error", interleave)
    result = mgr.complete(reason="all done", evidence=evidence)
    assert result["error_code"] == "state_changed"
    live = goals.load_goal("autonomy")
    assert live.status == "active" and "New delivery readback" in live.subgoals


def test_removal_quote_must_still_be_current_at_commit(home, monkeypatch):
    _, db = home
    mgr = manager()
    quote = "Remove 'Never restart the gateway'."
    db.append_message("autonomy", "user", quote)
    execute = db._execute_write

    def interleave(commit, *args, **kwargs):
        monkeypatch.setattr(db, "_execute_write", execute)
        db.append_message("autonomy", "user", "Keep that limit after all")
        return execute(commit, *args, **kwargs)

    monkeypatch.setattr(db, "_execute_write", interleave)
    result = mgr.revise(reason="remove limit", contract={"constraints": ""}, user_quote=quote)
    assert result["error_code"] == "state_changed"
    assert goals.load_goal("autonomy").contract.constraints == "Never restart the gateway"


def test_replacement_advances_the_instance_and_requires_fresh_readbacks(home):
    _, db = home
    mgr = manager()
    old_instance = mgr.state.created_at
    evidence = proof(db)
    assert mgr.replace(reason="objective changed", goal="Ship the alternate repair")["ok"]
    assert mgr.state.created_at > old_instance
    assert not mgr.complete(reason="old proof", evidence=evidence)["ok"]
    assert mgr.complete(reason="fresh proof", evidence=proof(db))["ok"]


def test_failed_migration_reports_persistence_failure_without_a_blocked_state(home, monkeypatch):
    _, db = home
    mgr = manager()
    mgr.state.status = "blocked"
    mgr.state.last_verdict = "blocked"
    goals.save_goal("autonomy", mgr.state)

    def unavailable(*args, **kwargs):
        raise OSError("store unavailable")

    monkeypatch.setattr(db, "_execute_write", unavailable)
    loaded = goals.GoalManager("autonomy")
    assert loaded.state.status == "active"
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "retry", False, None, False))
    result = loaded.evaluate_after_turn("continue")
    assert result["status"] == "active" and not result["should_continue"]
    assert result["verdict"] == "persistence_failed"


@pytest.mark.parametrize("action", ["replace", "revise", "subgoal_add"])
def test_an_implicit_outcome_cannot_be_replaced_by_an_easy_check(home, action):
    _, db = home
    mgr = goals.GoalManager("autonomy")
    mgr.set("Ship the full repair")
    if action == "replace":
        assert mgr.replace(reason="alternate route", goal="Check status")["ok"]
    elif action == "revise":
        assert mgr.revise(reason="add a check", contract={"verification": "Status checked"})["ok"]
    else:
        mgr.add_subgoal("Status checked")
    db.append_message("autonomy", "tool", "status is green", tool_name="terminal")
    result = mgr.complete(reason="only the easy check", evidence={"Status checked": "status is green"})
    assert not result["ok"] and "Ship the full repair" in result["error"]
    assert goals.load_goal("autonomy").status == "active"


def test_completion_rejects_negated_recorded_proof(home):
    _, db = home
    mgr = manager()
    db.append_message("autonomy", "tool", "No 12 passed result was observed", tool_name="terminal")
    result = mgr.complete(reason="not verified", evidence={
        "tests pass": "12 passed", "release readback matches": "12 passed"})
    assert not result["ok"] and "negated" in result["error"]
    assert goals.load_goal("autonomy").status == "active"


def test_no_prefixed_evidence_keeps_the_default_off_admission_contract(home):
    root, db = home
    mgr = manager()
    db.append_message("autonomy", "tool", "No 12 passed result was observed", tool_name="terminal")
    assert mgr._recorded_evidence_error("12 passed", mgr.state)["error_code"] == "evidence_negated"
    root.joinpath("config.yaml").write_text("goals:\n  autonomy: false\n", encoding="utf-8")
    assert mgr._recorded_evidence_error("12 passed", mgr.state) is None


@pytest.mark.parametrize("source", [
    "Do not remove 'Never restart the gateway'.",
    "If needed, remove 'Never restart the gateway'.",
    "Example: Remove 'Never restart the gateway'.",
    "Remove 'tests pass' and rename 'Never restart the gateway'.",
    "Remove 'Never restart the gateway'. Actually keep it.",
])
def test_quoted_target_data_cannot_supply_removal_authority(home, source):
    _, db = home
    mgr = manager()
    db.append_message("autonomy", "user", source)
    result = mgr.revise(reason="remove a limit", contract={"constraints": ""}, user_quote=source)
    assert result["error_code"] == "constraint_quote_required"
    assert goals.load_goal("autonomy").contract.constraints == "Never restart the gateway"
