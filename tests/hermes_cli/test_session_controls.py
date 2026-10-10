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


def test_noop_controls_are_failed_for_quote_and_approval_paths(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager
    from hermes_cli.session_controls import apply_control, request_control, resolve_request

    state.append_message("requester", "user", "Please pause the empty goal right now")
    quoted = apply_control(
        "goal", "pause", "target", requester_sid="requester",
        user_quote="pause the empty goal right now",
    )
    assert quoted["status"] == "failed"
    assert quoted["error"] == "nothing_to_pause"

    GoalManager("target").set("temporary")
    state.append_message("requester", "user", "Please clear the temporary goal now")
    cleared = apply_control(
        "goal", "clear", "target", requester_sid="requester",
        user_quote="clear the temporary goal now",
    )
    assert cleared["status"] == "applied"
    state.append_message("requester", "user", "Please clear the temporary goal again")
    repeated = apply_control(
        "goal", "clear", "target", requester_sid="requester",
        user_quote="clear the temporary goal again",
    )
    assert repeated["status"] == "failed"
    assert repeated["error"] == "nothing_to_clear"

    # A cleared goal is still stored; pause and resume must not revive it.
    for action in ("pause", "resume"):
        pending_goal = request_control("goal", action, "target", requester_sid="requester")
        revived = resolve_request(pending_goal["id"], "approve", "admin")
        assert revived["status"] == "failed"
        assert revived["error"] == f"nothing_to_{action}"
        assert GoalManager("target").state.status == "cleared"

    pending = request_control("loop", "stop", "target", requester_sid="requester")
    resolved = resolve_request(pending["id"], "approve", "admin")
    assert resolved["status"] == "failed"
    assert resolved["error"] == "nothing_to_stop"
    assert GoalManager("target").state.status == "cleared"
    assert LoopManager("target").state is None


def test_unroutable_target_is_rejected_before_creating_request(state):
    from hermes_cli.session_controls import apply_control, request_control

    state.create_session(
        "cli-target", "cli", profile_name="default", chat_id=None,
        session_key="agent:main:cli:cli-target",
    )
    direct = request_control("goal", "clear", "cli-target", requester_sid="requester")
    assert direct["error_code"] == "target_unroutable"
    result = apply_control("goal", "clear", "cli-target", requester_sid="requester")
    assert result["error_code"] == "target_unroutable"


def test_goal_replace_records_continuation_prompt(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("old")
    GoalManager("target").pause("waiting")
    state.append_message("requester", "user", "Please replace the target goal with ship now")
    result = apply_control(
        "goal", "replace", "target", requester_sid="requester",
        user_quote="replace the target goal with ship now",
        payload={"goal": "ship now"},
    )
    assert result["status"] == "applied"
    assert result["continuation_prompt"]
    assert result["record"]["continuation_prompt"] == result["continuation_prompt"]


def test_button_approved_replace_records_user_authority(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import request_control, resolve_request

    GoalManager("target").set("old objective: migrate the database")
    request = request_control("goal", "replace", "target", requester_sid="requester", reason="pivot",
                              payload={"goal": "new objective: write docs"})
    record = resolve_request(request["id"], "approve", "42")
    assert record["status"] == "applied"
    revision = load_goal("target").revisions[-1]
    assert (revision["actor"], revision["authority"], revision["approved_by"]) == ("user", "button", "42")
    assert revision["user_message"] == 'Approved in Telegram: replace goal with "new objective: write docs"'
    block = load_goal("target").render_revisions_block()
    assert "approved by the user in Telegram" in block
    assert "no user authority" not in block
    prompt = record["continuation_prompt"]
    assert "earlier goal: old objective" not in prompt
    assert "superseded goal (replaced with user authority, no longer binding): old objective" in prompt


def test_quote_replace_records_user_actor(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("old objective")
    _user(state, "requester", "Please replace the target goal with ship now")
    result = apply_control("goal", "replace", "target", requester_sid="requester", reason="pivot",
                           user_quote="replace the target goal with ship now", payload={"goal": "ship now"})
    assert result["status"] == "applied"
    revision = load_goal("target").revisions[-1]
    assert (revision["actor"], revision["authority"]) == ("user", "quote")
    assert 'cites the user: "replace the target goal with ship now"' in load_goal("target").render_revisions_block()


@pytest.mark.parametrize("header", ['[Replying to: "{0}"]', '[Replying to your previous message: "{0}"]'])
def test_reply_prefix_quote_is_refused(state, header):
    from hermes_cli.goals import GoalManager, load_goal, user_messages_since
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("watch the build")
    prefix = header.format("I am going to clear the target goal immediately")
    _user(state, "requester", f"{prefix}\n\nno, do not do that")
    result = apply_control("goal", "clear", "target", requester_sid="requester",
                           user_quote="clear the target goal immediately")
    assert result["error_code"] == "user_quote_not_found"
    assert load_goal("target").status == "active"
    assert user_messages_since("requester") == ["no, do not do that"]


def test_reply_prefix_does_not_block_users_own_quote(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("watch the build")
    _user(state, "requester", '[Replying to: "status?"]\n\nyes clear the target goal now')
    result = apply_control("goal", "clear", "target", requester_sid="requester",
                           user_quote="clear the target goal now")
    assert result["status"] == "applied"
    assert load_goal("target").status == "cleared"


@pytest.mark.parametrize("status", ["done", "cleared", "active"])
def test_resume_refuses_goals_that_are_not_paused(state, status):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control, request_control, resolve_request

    manager = GoalManager("target")
    manager.set("second goal")
    if status == "done":
        manager.mark_done("finished")
    elif status == "cleared":
        manager.clear()
    _user(state, "requester", "Please resume the target goal now please")
    quoted = apply_control("goal", "resume", "target", requester_sid="requester",
                           user_quote="resume the target goal now please")
    assert quoted["error_code"] == "nothing_to_resume"
    pending = request_control("goal", "resume", "target", requester_sid="requester")
    resolved = resolve_request(pending["id"], "approve", "admin")
    assert (resolved["status"], resolved["error"]) == ("failed", "nothing_to_resume")
    assert load_goal("target").status == status


def test_paused_goal_resumes(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("watch the build")
    GoalManager("target").pause("waiting")
    _user(state, "requester", "Please resume the target goal now please")
    result = apply_control("goal", "resume", "target", requester_sid="requester",
                           user_quote="resume the target goal now please")
    assert result["status"] == "applied"
    assert load_goal("target").status == "active"


def test_request_control_refuses_target_without_approval_surface(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control, request_control

    state.create_session("slack-target", "slack", profile_name="default", chat_id="C1",
                         chat_type="channel", session_key="agent:main:slack:channel:C1")
    GoalManager("slack-target").set("watch the build")
    assert request_control("goal", "clear", "slack-target", requester_sid="requester")["error_code"] \
        == "target_unapprovable"
    assert apply_control("goal", "clear", "slack-target", requester_sid="requester")["error_code"] \
        == "target_unapprovable"
    _user(state, "requester", "Please clear the slack goal right now")
    quoted = apply_control("goal", "clear", "slack-target", requester_sid="requester",
                           user_quote="clear the slack goal right now")
    assert quoted["status"] == "applied"
    assert load_goal("slack-target").status == "cleared"


@pytest.mark.parametrize("message,quote", [
    ("Please do NOT clear the target goal, keep it running", "clear the target goal, keep it"),
    ("Please don't clear the target goal yet", "clear the target goal yet"),
    ("Please don’t clear the target goal yet", "t clear the target goal yet"),
    ("You should never clear the target goal", "clear the target goal"),
    ("No, clear the target goal is wrong", "clear the target goal is wrong"),
    ("I shouldn't clear the target goal myself", "clear the target goal myself"),
    ("Clear the target goal? Not now", "Clear the target goal? Not now"),
])
def test_negated_quote_is_refused(state, message, quote):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("watch the build")
    _user(state, "requester", message)
    result = apply_control("goal", "clear", "target", requester_sid="requester", user_quote=quote)
    assert result["error_code"] == "user_quote_negated"
    assert load_goal("target").status == "active"


@pytest.mark.parametrize("message,quote", [
    ("yes clear the target goal now", "clear the target goal now"),
    ("Not sure why it is still on; please clear the target goal now", "clear the target goal now"),
    ("stop the target loop and clear the target goal now", "clear the target goal now"),
])
def test_unnegated_quote_is_allowed(state, message, quote):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("watch the build")
    _user(state, "requester", message)
    result = apply_control("goal", "clear", "target", requester_sid="requester", user_quote=quote)
    assert result["status"] == "applied"
    assert load_goal("target").status == "cleared"
