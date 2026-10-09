"""Regression tests for durable session controls."""
from __future__ import annotations

import pytest


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    from hermes_state_registry import acquire
    db = acquire(home / "state.db")
    for sid in ("requester", "target", "other"):
        db.create_session(sid, "telegram", profile_name="default", chat_id=sid,
                          chat_type="private", session_key=f"agent:main:telegram:dm:{sid}")
    goals._DB_CACHE[str(home)] = db
    yield db
    goals._DB_CACHE.clear()
    from hermes_state_registry import release_or_close
    release_or_close(db)


def _user(db, sid, text, *, display_kind=""):
    db.append_message(sid, "user", text, display_kind=display_kind)


def test_quote_applies_cross_session_clear_and_audits(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control, pending_outbox
    GoalManager("target").set("watch the build")
    _user(state, "requester", "Please clear the target goal immediately")
    result = apply_control("goal", "clear", "target", requester_sid="requester",
                           reason="finished", user_quote="clear the target goal immediately")
    assert result["status"] == "applied"
    assert load_goal("target").status == "cleared"
    assert result["record"]["authority"]["via"] == "quote"
    assert any(r["id"] == result["record"]["id"] for r in pending_outbox())


def test_stale_and_paraphrased_quotes_are_refused(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control
    GoalManager("target").set("watch the build")
    _user(state, "requester", "Please clear the target goal immediately")
    _user(state, "requester", "That earlier request is no longer current")
    stale = apply_control("goal", "clear", "target", requester_sid="requester",
                          reason="x", user_quote="clear the target goal immediately")
    paraphrase = apply_control("goal", "clear", "target", requester_sid="requester",
                               reason="x", user_quote="remove the target objective now")
    assert stale["error_code"] == "user_quote_not_found"
    assert paraphrase["error_code"] == "user_quote_not_found"
    assert GoalManager("target").state.status == "active"


def test_quote_from_different_session_and_relay_are_refused(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control
    GoalManager("target").set("watch the build")
    _user(state, "other", "Please clear the target goal immediately")
    different = apply_control("goal", "clear", "target", requester_sid="requester",
                              reason="x", user_quote="clear the target goal immediately")
    assert different["error_code"] == "user_quote_not_found"
    _user(state, "requester", "[relay from=other receipt=abc]\nPlease clear the target goal immediately")
    relay = apply_control("goal", "clear", "target", requester_sid="requester",
                          reason="x", user_quote="Please clear the target goal immediately")
    assert relay["error_code"] == "user_quote_not_found"


def test_no_quote_approval_deny_and_second_press_noop(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control, resolve_request
    GoalManager("target").set("watch the build")
    pending = apply_control("goal", "clear", "target", requester_sid="requester", reason="x")
    assert pending["status"] == "pending"
    denied = resolve_request(pending["request_id"], "deny", "admin")
    assert denied["status"] == "denied"
    assert resolve_request(pending["request_id"], "approve", "admin") is None
    assert GoalManager("target").state.status == "active"


def test_expire_request_uses_transaction_and_is_consume_once(state):
    import json
    from hermes_cli import session_controls

    request = session_controls.request_control("goal", "clear", "target", requester_sid="requester")
    key = session_controls._record_key(request["id"])
    record = json.loads(state.get_meta(key))
    record["expires_at"] = 0
    state.set_meta(key, json.dumps(record))
    expired = session_controls.expire_request(request["id"])
    assert expired["status"] == "expired"
    assert session_controls.expire_request(request["id"]) is None
    assert json.loads(state.get_meta(key))["status"] == "expired"


def test_session_controls_do_not_pollute_goal_or_loop_revisions(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.loops import LoopManager, load_loop
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("watch the build")
    _user(state, "requester", "Please pause the target goal right now")
    before_goal = len(load_goal("target").revisions)
    result = apply_control("goal", "pause", "target", requester_sid="requester",
                           user_quote="pause the target goal right now")
    assert result["status"] == "applied"
    assert len(load_goal("target").revisions) == before_goal

    LoopManager("target").set("check status")
    before_loop = len(load_loop("target").revisions)
    _user(state, "requester", "Please pause the target loop right now")
    result = apply_control("loop", "pause", "target", requester_sid="requester",
                           user_quote="pause the target loop right now")
    assert result["status"] == "applied"
    assert len(load_loop("target").revisions) == before_loop


def test_stale_applying_request_is_recovered_as_interrupted(state):
    import json
    import time
    from hermes_cli import session_controls

    request = session_controls.request_control("goal", "clear", "target", requester_sid="requester")
    key = session_controls._record_key(request["id"])
    record = json.loads(state.get_meta(key))
    record["status"] = "applying"
    record["resolved_at"] = time.time() - 11 * 60
    state.set_meta(key, json.dumps(record))
    pending = session_controls.pending_outbox()
    recovered = next(item for item in pending if item["id"] == request["id"])
    assert recovered["status"] == "failed"
    assert recovered["error"] == "interrupted"


def test_loop_controls_and_goal_replace(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager, load_loop
    from hermes_cli.session_controls import apply_control, resolve_request
    _user(state, "requester", "Please update the goal to ship the release now")
    GoalManager("target").set("old")
    replaced = apply_control("goal", "replace", "target", requester_sid="requester", reason="user request",
                             user_quote="update the goal to ship the release now",
                             payload={"goal": "ship the release now"})
    assert replaced["status"] == "applied"
    assert replaced["state"].goal == "ship the release now"
    LoopManager("target").set("check status")
    pending = apply_control("loop", "pause", "target", requester_sid="requester", reason="x")
    assert resolve_request(pending["request_id"], "approve", "admin")["status"] == "applied"
    assert load_loop("target").status == "paused"
    assert apply_control("loop", "resume", "target", requester_sid="requester", reason="x")["status"] == "pending"


def test_target_resolution_errors(state):
    from hermes_cli.session_controls import resolve_target
    assert resolve_target("target") == "target"
    with pytest.raises(ValueError, match="unknown_target"):
        resolve_target("missing")
    with pytest.raises(ValueError, match="cross_profile"):
        resolve_target("hermes:other/target")
