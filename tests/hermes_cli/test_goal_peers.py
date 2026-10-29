"""A peer result remains a parent-owned dependency until recorded verification."""
import json
import time

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import goals
from hermes_cli.checkins import CheckinConfig, CheckinManager, outstanding_work
from hermes_cli.goal_command import dispatch_goal_command
from hermes_state import SessionDB

PEER = "hermes:default/peer"
PROOF = "pytest peer_artifact.py: 1 passed, artifact readback verified"


@pytest.fixture
def parent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("goals:\n  autonomy: true\n")
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", source="telegram")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    monkeypatch.setattr("tools.async_delegation.supervisor_inventory", lambda _: [])
    mgr = goals.GoalManager("parent")
    mgr.set("Deliver the verified peer artifact")
    with scoped_current_session_id("parent"):
        yield mgr, db
    db.close()


def command(mgr, text):
    return dispatch_goal_command(mgr, text, authorize_gate=lambda: None)


def register(mgr, peer=PEER, ref="existing-work"):
    result = mgr.wait_on_peer(peer, ref, "artifact readback", expected_instance=mgr.state.created_at)
    assert result["ok"], result


def test_peer_only_handoff_survives_idle_and_unverified_result(parent):
    mgr, db = parent
    instance = mgr.state.created_at
    result = command(mgr, f'peer wait {instance} hermes:default/peer existing-work "artifact readback"')
    assert not result.error, result.output
    work = outstanding_work(db, "parent")
    assert len(work) == 1 and work[0]["kind"] == "peer"
    checkin = CheckinManager(db, "parent", CheckinConfig())
    assert checkin.reconcile(work, now=100).due_at == 1300
    # Neither idle nor a peer's prose is parent tool proof.
    db.create_session("peer", source="telegram")
    db.append_message("peer", "assistant", "artifact readback verified")
    before = db.get_meta("goal:parent")
    rejected = command(mgr, f'peer resolve {instance} hermes:default/peer existing-work "artifact readback verified"')
    assert rejected.error
    assert db.get_meta("goal:parent") == before
    assert outstanding_work(db, "parent")[0]["status"] == "awaiting parent verification"


def test_resolve_only_exact_ref_with_recorded_parent_proof(parent):
    mgr, db = parent
    register(mgr)
    register(mgr, "hermes:other/second", "second-work")
    db.append_message("parent", "tool", PROOF, tool_name="terminal")
    instance = mgr.state.created_at
    result = command(mgr, f'peer resolve {instance} {PEER} existing-work "{PROOF}"')
    assert not result.error, result.output
    loaded = goals.GoalManager("parent")
    assert loaded.state.pending_peers == [{"peer": "hermes:other/second", "ref": "second-work", "result": "artifact readback"}]
    record = loaded.state.revisions[-1]
    assert (record["kind"], record["peer"], record["ref"], record["evidence"]) == (
        "peer_resolved", PEER, "existing-work", PROOF)
    assert "second-work" in command(mgr, "peer list").output
    assert not db.list_meta_prefix("loop:")


def test_peer_resolution_refuses_native_cas_conflict_without_losing_concurrent_peer(parent, monkeypatch):
    from hermes_cli import goals_evaluation
    mgr, db = parent
    register(mgr)
    db.append_message("parent", "tool", PROOF, tool_name="terminal")
    commit = goals_evaluation.commit_goal_snapshot
    raced = False

    def race(session_id, expected, state, database, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            register(goals.GoalManager("parent"), ref="concurrent-work")
        return commit(session_id, expected, state, database, **kwargs)

    monkeypatch.setattr(goals_evaluation, "commit_goal_snapshot", race)
    result = mgr.resolve_peer(PEER, "existing-work", PROOF, expected_instance=mgr.state.created_at)
    assert result.get("error_code") == "state_changed", result
    current = goals.load_goal("parent")
    assert current is not None
    assert {row["ref"] for row in current.pending_peers} == {"existing-work", "concurrent-work"}
    assert mgr.resolve_peer(PEER, "existing-work", PROOF, expected_instance=mgr.state.created_at)["ok"]
    assert [row["ref"] for row in goals.load_goal("parent").pending_peers] == ["concurrent-work"]
    assert raced


@pytest.mark.parametrize("case", ["foreign", "profile", "stale", "ref", "peer", "missing", "peer-only", "assistant", "old-proof", "negated", "excluded", "generic"])
def test_rejected_resolution_does_not_mutate(parent, monkeypatch, case):
    mgr, db = parent
    register(mgr)
    instance, peer, ref, proof = mgr.state.created_at, PEER, "existing-work", PROOF
    sid, role, tool, timestamp = "parent", "tool", "terminal", time.time()
    if case == "foreign":
        caller = "some-other-parent"
    else:
        caller = "parent"
    if case == "profile":
        monkeypatch.setenv("HERMES_SESSION_PROFILE", "foreign")
        monkeypatch.setattr("hermes_cli.session_controls._current_profile", lambda: "default")
    if case == "stale":
        instance -= 1
    if case == "ref":
        ref = "wrong-work"
    if case == "peer":
        peer = "hermes:default/reassigned"
    if case == "peer-only":
        sid = "peer"
        db.create_session(sid, source="telegram")
    if case == "assistant":
        role = "assistant"
    if case == "old-proof":
        timestamp = mgr.state.created_at - 1
    if case == "excluded":
        tool = "goal_set"
    if case == "generic":
        proof = "all checks passed"
    if case != "missing":
        content = "not " + proof if case == "negated" else proof
        db.append_message(sid, role, content, tool_name=tool, timestamp=timestamp)
    before = db.get_meta("goal:parent")
    from gateway.session_context import preserved_session_context, _SESSION_PROFILE
    with preserved_session_context(), scoped_current_session_id(caller):
        if case == "profile":
            _SESSION_PROFILE.set("foreign")
        result = mgr.resolve_peer(peer, ref, proof, expected_instance=instance)
    assert not result["ok"], case
    assert db.get_meta("goal:parent") == before


@pytest.mark.parametrize("peer", ["peer", "hermes:default", "hermes:default/peer/more", "hermes:default/peer "])
def test_registration_rejects_nonliteral_address(parent, peer):
    mgr, db = parent
    before = db.get_meta("goal:parent")
    with pytest.raises(ValueError, match="exact"):
        register(mgr, peer)
    assert db.get_meta("goal:parent") == before


def test_multiple_peers_survive_wait_reset_repark_resume_and_restart(parent):
    mgr, db = parent
    register(mgr)
    register(mgr, ref="other-work")
    expected = list(mgr.state.pending_peers)
    for action in (lambda: mgr.wait_for_seconds(1200), mgr.stop_waiting,
                   lambda: mgr.wait_on_session("peer"), lambda: mgr.clear_lifted_wait(mgr.state.waiting_since),
                   lambda: mgr.pause("user-paused"), mgr.resume,
                   lambda: mgr.wait_for_seconds(60), mgr.resume):
        action()
        assert goals.load_goal("parent").pending_peers == expected
    assert goals.GoalManager("parent").state.pending_peers == expected
    assert mgr.replace(goal="Use the alternate route", reason="same deliverable")["ok"]
    assert goals.load_goal("parent").pending_peers == expected
    with pytest.raises(ValueError, match="pending peer"):
        mgr.set("An unrelated replacement")
    before = db.get_meta("goal:parent")
    register(mgr)  # idempotent handoff, not a cadence reset or duplicate row
    assert db.get_meta("goal:parent") == before


def test_compression_migrates_dependencies_but_not_fork_authority(parent):
    mgr, db = parent
    register(mgr)
    register(mgr, "hermes:other/second", "second-work")
    instance = mgr.state.created_at
    db.end_session("parent", "compression")
    db.create_session("tip", source="telegram", parent_session_id="parent")
    assert goals.migrate_goal_to_session("parent", "tip", reason="compression")
    assert {row["id"] for row in outstanding_work(db, "tip")} == {"existing-work", "second-work"}
    tip = goals.GoalManager("tip")
    db.append_message("tip", "tool", PROOF, tool_name="terminal")
    with scoped_current_session_id("tip"):
        assert tip.resolve_peer(PEER, "existing-work", PROOF, expected_instance=instance)["ok"]
        assert len(goals.load_goal("tip").pending_peers) == 1
        assert tip.resolve_peer("hermes:other/second", "second-work", PROOF, expected_instance=instance)["ok"]
    assert goals.load_goal("tip").pending_peers == []
    assert not outstanding_work(db, "tip")
    assert not mgr.resolve_peer(PEER, "existing-work", PROOF, expected_instance=instance)["ok"]
    db.create_session("fork", source="telegram", parent_session_id="tip")
    assert goals.load_goal("fork") is None


@pytest.mark.parametrize("route", ["manager", "mutation", "autonomous", "judge"])
def test_pending_work_blocks_all_completion_routes(parent, monkeypatch, route):
    mgr, db = parent
    register(mgr)
    before = db.get_meta("goal:parent")
    if route == "manager":
        with pytest.raises(ValueError, match="pending peer"):
            mgr.mark_done("peer said done")
    elif route == "mutation":
        with pytest.raises(ValueError, match="pending peer"):
            mgr._mutate_current(lambda state: setattr(state, "status", "done"))
    elif route == "autonomous":
        result = mgr.complete(reason="peer said done", evidence=PROOF)
        assert not result["ok"] and result["error_code"] == "pending_peers"
    else:
        monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("done", "peer said done", True, None, False))
        monkeypatch.setattr("hermes_cli.goals_autonomy.goal_autonomy_enabled", lambda: False)
        monkeypatch.setattr(goals, "goal_autonomy_enabled", lambda: False)
        result = mgr.evaluate_after_turn("peer said done")
        assert not result["should_continue"] and result["status"] != "done"
        assert goals.load_goal("parent").pending_peers
        assert goals.load_goal("parent").status == "active"
        return
    assert db.get_meta("goal:parent") == before


def test_final_resolution_allows_completion_and_disarms(parent):
    mgr, db = parent
    register(mgr)
    checkin = CheckinManager(db, "parent", CheckinConfig())
    checkin.reconcile(outstanding_work(db, "parent"), now=100)
    db.append_message("parent", "tool", PROOF, tool_name="terminal")
    assert mgr.resolve_peer(PEER, "existing-work", PROOF, expected_instance=mgr.state.created_at)["ok"]
    mgr.mark_done("verified by parent")
    assert goals.load_goal("parent").status == "done"
    assert checkin.reconcile(outstanding_work(db, "parent")) is None


def test_native_controls_preserve_refs_and_refuse_agent_clear(parent, monkeypatch):
    from hermes_cli import session_controls
    mgr, db = parent
    monkeypatch.setattr(session_controls, "_db", lambda: db)
    register(mgr)
    for action, payload in [("pause", None),
                            ("replace", {"goal": "Use another route for the artifact"})]:
        result = session_controls.apply_control("goal", action, "parent", requester_sid="parent",
                                                reason="continue the same work", payload=payload)
        assert result["ok"], result
        assert len(goals.load_goal("parent").pending_peers) == 1
    result = session_controls.apply_control("goal", "clear", "parent", requester_sid="parent")
    assert not result["ok"]
    assert len(goals.load_goal("parent").pending_peers) == 1


def test_explicit_user_clear_retains_existing_semantics(parent):
    mgr, db = parent
    register(mgr)
    mgr.pause("user-paused")
    assert not outstanding_work(db, "parent")
    mgr.resume()
    assert outstanding_work(db, "parent")
    mgr.clear()
    assert not outstanding_work(db, "parent")
    assert json.loads(db.get_meta("goal:parent"))["status"] == "cleared"


@pytest.mark.parametrize("arg", [
    'wait invalid hermes:default/peer existing-work "artifact readback"',
    'resolve invalid hermes:default/peer existing-work "artifact readback"',
    'wait nan hermes:default/peer existing-work "artifact readback"',
    'wait inf hermes:default/peer existing-work "artifact readback"',
    'wait 123 hermes:default/peer existing-work "unclosed',
])
def test_malformed_peer_input_returns_usage_without_mutation(parent, arg):
    from hermes_cli.goal_peer_command import USAGE

    mgr, db = parent
    before = db.get_meta("goal:parent")
    result = command(mgr, "peer " + arg)
    assert result.error and result.output == USAGE
    assert db.get_meta("goal:parent") == before
