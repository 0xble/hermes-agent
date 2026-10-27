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


def test_stale_and_paraphrased_self_quotes_are_refused(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control
    GoalManager("requester").set("watch the build")
    _user(state, "requester", "Please clear the requester goal immediately")
    _user(state, "requester", "That earlier request is no longer current")
    stale = apply_control("goal", "clear", "requester", requester_sid="requester",
                          reason="x", user_quote="clear the requester goal immediately")
    paraphrase = apply_control("goal", "clear", "requester", requester_sid="requester",
                               reason="x", user_quote="remove the requester objective now")
    assert (stale["status"], stale["quote_refused"]) == ("pending", "user_quote_not_found")
    assert (paraphrase["status"], paraphrase["quote_refused"]) == ("pending", "user_quote_not_found")
    assert GoalManager("requester").state.status == "active"


def test_quote_overtaken_before_write_lock_falls_back_to_approval(state, monkeypatch):
    """A self-targeted quote valid at first check but stale under the write lock still requests approval."""
    from hermes_cli.goals import GoalManager
    from hermes_cli import session_controls
    from hermes_cli.session_controls import apply_control, pending_outbox
    GoalManager("requester").set("watch the build")
    _user(state, "requester", "Please clear the requester goal immediately")
    real = session_controls.check_user_quote

    def overtaken(sid, quote, *, cursor=None):
        if cursor is not None:
            return "user_quote_not_found"
        return real(sid, quote)

    monkeypatch.setattr(session_controls, "check_user_quote", overtaken)
    result = apply_control("goal", "clear", "requester", requester_sid="requester",
                           reason="x", user_quote="clear the requester goal immediately")
    assert (result["status"], result["quote_refused"]) == ("pending", "user_quote_not_found")
    assert [r["id"] for r in pending_outbox()] == [result["request_id"]]
    assert GoalManager("requester").state.status == "active"


def test_cross_session_goal_changes_do_not_wait_for_approval(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control, pending_outbox

    GoalManager("target").set("watch the build")
    cleared = apply_control("goal", "clear", "target", requester_sid="requester", reason="finished")
    assert cleared["status"] == "applied"
    assert cleared["record"]["authority"]["via"] == "session"
    assert load_goal("target").status == "cleared"
    assert not [record for record in pending_outbox() if record.get("status") == "pending"]

    GoalManager("target").set("old objective")
    replaced = apply_control(
        "goal", "replace", "target", requester_sid="requester", reason="pivot",
        payload={"goal": "new objective"},
    )
    assert replaced["status"] == "applied"
    assert replaced["record"]["authority"]["via"] == "session"
    assert load_goal("target").goal == "new objective"


def test_cross_session_goal_replace_without_an_active_goal_fails_before_any_request(state):
    from hermes_cli.goals import load_goal
    from hermes_cli.session_controls import apply_control, pending_outbox

    result = apply_control(
        "goal", "replace", "target", requester_sid="requester", reason="pivot",
        payload={"goal": "new objective"},
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error_code"] == "no_goal"
    assert result["error"] == "there is no active or paused goal to replace"
    assert result["state"] is None
    assert result["revision"] is None
    assert "record" not in result
    assert load_goal("target") is None
    assert not [record for record in pending_outbox() if record.get("status") == "pending"]

    self_result = apply_control(
        "goal", "replace", "target", requester_sid="target", user_quote="replace it",
        payload={"goal": "self replacement"},
    )
    assert self_result["error_code"] == "no_goal"
    assert self_result["status"] == "failed"


def test_cross_session_replace_rechecks_goal_before_recording_failure(state, monkeypatch):
    from hermes_cli import session_controls
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control, pending_outbox

    GoalManager("target").set("old objective")
    calls = 0

    def race(target_sid, *, cursor=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return {
            "ok": False,
            "status": "failed",
            "error_code": "no_goal",
            "error": "there is no active or paused goal to replace",
            "state": None,
            "revision": None,
        }

    monkeypatch.setattr(session_controls, "_goal_replace_failure", race)
    result = apply_control(
        "goal", "replace", "target", requester_sid="requester", reason="pivot",
        payload={"goal": "new objective"},
    )

    assert calls == 2
    assert result["error_code"] == "no_goal"
    assert result["status"] == "failed"
    assert "record" not in result
    installed = load_goal("target")
    assert installed is not None
    assert installed.goal == "old objective"
    assert pending_outbox() == []


def test_self_goal_change_approval_is_non_blocking_and_second_press_noop(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control, resolve_request

    GoalManager("requester").set("watch the build")
    pending = apply_control("goal", "clear", "requester", requester_sid="requester", reason="x")
    assert pending["status"] == "pending"
    assert pending["non_blocking"] is True
    denied = resolve_request(pending["request_id"], "deny", "admin")
    assert denied["status"] == "denied"
    assert resolve_request(pending["request_id"], "approve", "admin") is None
    assert GoalManager("requester").state.status == "active"


def test_cross_session_goal_change_with_refused_quote_still_uses_approval(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("watch the build")
    _user(state, "requester", "[relay from=other receipt=abc]\nPlease clear the target goal immediately")
    result = apply_control("goal", "clear", "target", requester_sid="requester",
                           reason="x", user_quote="Please clear the target goal immediately")
    assert result["status"] == "pending"
    assert result["quote_refused"] == "user_quote_not_found"
    assert load_goal("target").status == "active"


def test_no_quote_approval_applies_clear_to_unchanged_goal(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control, resolve_request

    GoalManager("target").set("watch the build")
    pending = apply_control("goal", "clear", "target", requester_sid="target", reason="x")
    assert pending["status"] == "pending"

    applied = resolve_request(pending["request_id"], "approve", "admin")

    assert applied is not None
    assert applied["status"] == "applied"
    assert load_goal("target").status == "cleared"


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


def test_resolve_request_reports_expired_record_for_callback_feedback(state):
    import json
    from hermes_cli import session_controls

    request = session_controls.request_control("goal", "clear", "target", requester_sid="requester")
    key = session_controls._record_key(request["id"])
    record = json.loads(state.get_meta(key))
    record["expires_at"] = 0
    state.set_meta(key, json.dumps(record))

    resolved = session_controls.resolve_request(request["id"], "approve", "admin")

    assert resolved is not None
    assert resolved["status"] == "expired"
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


def test_loop_controls_and_goal_replace(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager, load_loop
    from hermes_cli.session_controls import apply_control, resolve_request
    _user(state, "requester", "Please update the target goal to ship the release now")
    GoalManager("target").set("old")
    replaced = apply_control("goal", "replace", "target", requester_sid="requester", reason="user request",
                             user_quote="update the target goal to ship the release now",
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

    state.append_message("requester", "user", "Please pause the target goal right now")
    quoted = apply_control(
        "goal", "pause", "target", requester_sid="requester",
        user_quote="pause the target goal right now",
    )
    assert quoted["status"] == "failed"
    assert quoted["error"] == "nothing_to_pause"

    GoalManager("target").set("temporary")
    state.append_message("requester", "user", "Please clear the target goal now")
    cleared = apply_control(
        "goal", "clear", "target", requester_sid="requester",
        user_quote="clear the target goal now",
    )
    assert cleared["status"] == "applied"
    state.append_message("requester", "user", "Please clear the target goal again")
    repeated = apply_control(
        "goal", "clear", "target", requester_sid="requester",
        user_quote="clear the target goal again",
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
    # A card without a target snapshot is never approval authority, even for a no-op.
    assert resolved["error"] == "target_changed"
    assert GoalManager("target").state.status == "cleared"
    assert LoopManager("target").state is None


def test_cross_session_goal_change_can_apply_to_a_local_target(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control, request_control

    state.create_session(
        "cli-target", "cli", profile_name="default", chat_id=None,
        session_key="agent:main:cli:cli-target",
    )
    GoalManager("cli-target").set("watch the build")
    direct = request_control("goal", "clear", "cli-target", requester_sid="requester")
    assert direct["error_code"] == "target_unroutable"
    result = apply_control("goal", "clear", "cli-target", requester_sid="requester")
    assert result["status"] == "applied"
    assert load_goal("cli-target").status == "cleared"


def test_goal_replace_records_continuation_prompt(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("old")
    state.append_message("requester", "user", "Please replace the target goal with ship now")
    result = apply_control(
        "goal", "replace", "target", requester_sid="requester",
        user_quote="replace the target goal with ship now",
        payload={"goal": "ship now"},
    )
    assert result["status"] == "applied"
    assert result["continuation_prompt"]
    assert result["record"]["continuation_prompt"] == result["continuation_prompt"]


@pytest.mark.parametrize("via", ["quote", "button", "session"])
@pytest.mark.parametrize("replacement_count", [1, 2])
def test_replacement_prompts_bind_only_the_current_contract(state, via, replacement_count):
    """Explicitly removed/replaced requirements stay audit-only, not binding prompt history.

    Omission preserves requirements, so this test uses explicit empty contract edits and the
    existing user subgoal-removal control rather than relying on an objective change to erase them.
    """
    import copy
    from unittest.mock import MagicMock, patch
    from hermes_cli.goals import GoalContract, GoalManager, judge_goal, load_goal
    from hermes_cli.session_controls import apply_control, resolve_request

    target_sid = "requester" if via == "button" else "target"
    manager = GoalManager(target_sid)
    manager.set("migrate production", contract=GoalContract(
        verification="production migration verified", constraints="do not lose production data",
        boundaries="production shards only", stop_when="production unavailable",
    ))
    manager.add_subgoal("production shard")
    assert manager.revise(reason="reword verification", contract={
        "verification": "production migration verified. Signed migration receipt captured",
    })["ok"]
    original_revision = copy.deepcopy(manager.state.revisions[0])

    def replace(goal, verification):
        quote = f"replace the target goal with {goal}"
        if via == "quote":
            _user(state, "requester", f"Please {quote}")
        result = apply_control(
            "goal", "replace", target_sid, requester_sid="requester", reason="user pivot",
            user_quote=quote if via == "quote" else "",
            payload={"goal": goal, "contract": {"outcome": "", "verification": verification,
                                                 "constraints": "", "boundaries": "", "stop_when": ""}},
        )
        if via == "button":
            assert result["status"] == "pending"
            record = resolve_request(result["request_id"], "approve", "42")
        else:
            record = result["record"]
        assert record["status"] == "applied"
        return GoalManager(target_sid)

    assert manager.remove_subgoal(1) == "production shard"
    manager = replace("write a local design note", "design note exists")
    manager.add_subgoal("include local examples")
    assert manager.revise(reason="reword local verification", contract={
        "verification": "design note exists. Peer reviewed",
    })["ok"]
    if replacement_count == 2:
        assert manager.remove_subgoal(1) == "include local examples"
        manager = replace("write a local checklist", "local checklist exists")
        assert manager.state.subgoals == []
        assert manager.revise(reason="reword checklist verification", contract={
            "verification": "local checklist exists. Peer reviewed",
        })["ok"]
    history = copy.deepcopy(manager.state.revisions)
    assert history[0] == original_revision
    assert len(history) == 3 * replacement_count + 1
    removals = [row for row in history if row.get("kind") == "remove"]
    assert len(removals) == replacement_count
    for row, item in zip(removals, ("production shard", "include local examples")):
        assert (row["actor"], row["authority"]) == ("user", "explicit_command")
        assert row["before"] == {"subgoals": [item]}
        assert row["after"] == {"subgoals": []}
    assert manager.state.revisions[-1].get("actor") == "agent"

    with patch("agent.auxiliary_client.call_llm", return_value=MagicMock(
        choices=[MagicMock(message=MagicMock(content='{"verdict": "continue", "reason": "more"}'))],
    )) as call_llm:
        judge_goal(manager.state.goal, "still working", contract=manager.state.contract,
                   subgoals=manager.state.subgoals)
    prompts = [manager.next_continuation_prompt(), call_llm.call_args.kwargs["messages"][1]["content"]]
    for prompt in prompts:
        for text in ("production migration verified", "do not lose production data", "production shard",
                     "production unavailable"):
            assert text not in prompt
        if replacement_count == 2:
            assert "local checklist exists. Peer reviewed" in prompt
            assert "design note exists" not in prompt and "include local examples" not in prompt
        else:
            assert "design note exists. Peer reviewed" in prompt and "include local examples" in prompt
        for marker in ("This goal has been revised", "Revision history", "superseded", "no user authority",
                       "earlier verification"):
            assert marker not in prompt
    # Prompt building changes no audit data.
    assert manager.state.revisions == history
    assert load_goal(target_sid).revisions == history


def test_session_replacement_resets_resume_override(state):
    from unittest.mock import patch
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control

    manager = GoalManager("target")
    manager.set("continue despite the blocker")
    assert manager.state is not None
    with patch.object(goals, "judge_goal", return_value=(
        "blocked", "the irreversible no-delete constraint would be breached", False, None, False,
    )):
        assert manager.evaluate_after_turn("I cannot proceed")["verdict"] == "blocked"
    assert manager.resume() is not None
    assert manager.state.has_resume_override() is True

    result = apply_control(
        "goal", "replace", "target", requester_sid="requester", reason="pivot",
        payload={"goal": "ship the release"},
    )
    assert result["status"] == "applied"
    assert result["record"]["authority"]["via"] == "session"
    replaced_state = GoalManager("target").state
    assert replaced_state is not None
    assert replaced_state.has_resume_override() is False


def test_button_approved_replace_records_user_authority(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import request_control, resolve_request

    GoalManager("requester").set("old objective: migrate the database")
    request = request_control("goal", "replace", "requester", requester_sid="requester", reason="pivot",
                              payload={"goal": "new objective: write docs"})
    record = resolve_request(request["id"], "approve", "42")
    assert record["status"] == "applied"
    revision = load_goal("requester").revisions[-1]
    assert (revision["actor"], revision["authority"], revision["approved_by"]) == ("user", "button", "42")
    assert revision["user_message"] == 'Approved in Telegram: replace goal with "new objective: write docs"'
    prompt = record["continuation_prompt"]
    assert "new objective: write docs" in prompt
    assert "old objective" not in prompt


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
    assert revision["user_quote"] == "replace the target goal with ship now"


def test_direct_manager_quote_replace_records_user_authority(state):
    """A quote supplied without a command or approval button cannot authorize replacement."""
    from hermes_cli.goals import GoalManager, load_goal

    GoalManager("target").set("migrate database")
    _user(state, "target", "Please replace the goal with ship the release")
    result = GoalManager("target").replace(reason="pivot", goal="ship the release",
                                           user_quote="replace the goal with ship the release")
    assert result["error_code"] == "destructive_change_requires_command"
    assert load_goal("target").goal == "migrate database"


@pytest.mark.parametrize("header", ['[Replying to: "{0}"]', '[Replying to your previous message: "{0}"]'])
def test_reply_prefix_quote_is_refused(state, header):
    from hermes_cli.goals import GoalManager, load_goal, user_messages_since
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("watch the build")
    prefix = header.format("I am going to clear the target goal immediately")
    _user(state, "requester", f"{prefix}\n\nno, do not do that")
    result = apply_control("goal", "clear", "target", requester_sid="requester",
                           user_quote="clear the target goal immediately")
    assert (result["status"], result["quote_refused"]) == ("pending", "user_quote_not_found")
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
    direct = apply_control("goal", "clear", "slack-target", requester_sid="requester")
    assert direct["status"] == "applied"
    assert load_goal("slack-target").status == "cleared"
    GoalManager("slack-target").set("watch the build again")
    _user(state, "requester", "Please clear the slack-target goal right now")
    quoted = apply_control("goal", "clear", "slack-target", requester_sid="requester",
                           user_quote="clear the slack-target goal right now")
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
    ("Please do **not** clear the target goal immediately", "clear the target goal immediately"),
    ("Please do *not* clear the target goal immediately", "clear the target goal immediately"),
    ("Please do _not_ clear the target goal immediately", "clear the target goal immediately"),
    ("Please `don't` clear the target goal immediately", "clear the target goal immediately"),
    ("Please do ~~not~~ clear the target goal immediately", "clear the target goal immediately"),
    ("Please do**not**clear the target goal immediately", "clear the target goal immediately"),
    ("Please do not, under any circumstances, clear the target goal immediately.",
     "clear the target goal immediately"),
    ("Do not\u2014under any circumstances\u2014clear the target goal", "clear the target goal"),
    ("Do not\u2013ever\u2013clear the target goal", "clear the target goal"),
    ("Don't/never clear the target goal", "clear the target goal"),
    ("Never, even if the build passes and the deploy is green, clear the target goal",
     "clear the target goal"),
])
def test_negated_quote_is_refused(state, message, quote):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("watch the build")
    _user(state, "requester", message)
    result = apply_control("goal", "clear", "target", requester_sid="requester", user_quote=quote)
    assert (result["status"], result["quote_refused"]) == ("pending", "user_quote_negated")
    assert load_goal("target").status == "active"


@pytest.mark.parametrize("message,quote", [
    ("yes clear the target goal now", "clear the target goal now"),
    ("Please clear the target goal now.", "clear the target goal now"),
    ("Clear the target goal now!", "Clear the target goal now"),
])
def test_unnegated_quote_is_allowed(state, message, quote):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("watch the build")
    _user(state, "requester", message)
    result = apply_control("goal", "clear", "target", requester_sid="requester", user_quote=quote)
    assert result["status"] == "applied"
    assert load_goal("target").status == "cleared"


def test_approval_clear_applies_to_the_current_goal_after_revision(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.loops import LoopManager, load_loop
    from hermes_cli.session_controls import request_control, resolve_request

    GoalManager("requester").set("old objective")
    request = request_control("goal", "clear", "requester", requester_sid="requester")
    GoalManager("requester").clear()
    GoalManager("requester").set("a different objective")
    resolved = resolve_request(request["id"], "approve", "admin")
    assert resolved is not None
    assert resolved["status"] == "applied"
    assert load_goal("requester").status == "cleared"
    assert load_goal("requester").goal == "a different objective"
    assert resolve_request(request["id"], "approve", "admin") is None

    LoopManager("target").set("check the build")
    loop_request = request_control("loop", "stop", "target", requester_sid="requester")
    LoopManager("target").clear()
    LoopManager("target").set("check a different thing")
    stopped = resolve_request(loop_request["id"], "approve", "admin")
    assert stopped["error"] == "target_changed"
    assert load_loop("target").prompt == "check a different thing"

    GoalManager("target").set("pause this goal")
    unchanged = request_control("goal", "pause", "target", requester_sid="requester")
    assert resolve_request(unchanged["id"], "approve", "admin")["status"] == "applied"
    assert load_goal("target").status == "paused"


@pytest.mark.parametrize("kind,action", [("goal", "clear"), ("loop", "stop")])
def test_approval_blocks_cross_process_definition_swap_at_apply_boundary(state, monkeypatch, kind, action):
    import json
    import subprocess
    import sys
    from hermes_cli import session_controls
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.loops import LoopManager, load_loop

    manager_type, load = (GoalManager, load_goal) if kind == "goal" else (LoopManager, load_loop)
    manager_type("target").set("old definition")
    request = session_controls.request_control(kind, action, "target", requester_sid="requester")
    key = f"{kind}:target"
    replacement = json.loads(state.get_meta(key))
    replacement["goal" if kind == "goal" else "prompt"] = "new definition"
    replacement["created_at"] += 1
    script = (
        "import sqlite3,sys; c=sqlite3.connect(sys.argv[1], timeout=0); "
        "c.execute('UPDATE state_meta SET value=? WHERE key=?', (sys.argv[3],sys.argv[2])); c.commit()"
    )
    attempts = []
    original = session_controls._manager_apply

    def at_boundary(*args, **kwargs):
        attempt = subprocess.run(
            [sys.executable, "-c", script, str(state.db_path), key, json.dumps(replacement)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        attempts.append(attempt)
        return original(*args, **kwargs)

    monkeypatch.setattr(session_controls, "_manager_apply", at_boundary)
    resolved = session_controls.resolve_request(request["id"], "approve", "admin")
    assert len(attempts) == 1
    assert attempts[0].returncode != 0, "replacement won the gap after approval validation"
    assert "database is locked" in attempts[0].stderr
    assert resolved["status"] == "applied"
    assert load("target").status == "cleared"
    assert getattr(load("target"), "goal" if kind == "goal" else "prompt") == "old definition"
    # The other process can replace after commit; approval cannot subsequently clear its row.
    subprocess.run([sys.executable, "-c", script, str(state.db_path), key, json.dumps(replacement)],
                   check=True, capture_output=True, timeout=10)
    assert load("target").status == "active"


@pytest.mark.parametrize("kind,field,value", [
    ("goal", "contract", {"constraints": "never deploy"}),
    ("goal", "subgoals", ["additional criterion"]),
    ("goal", "revisions", [{"reason": "changed contract"}]),
    ("goal", "max_turns", 77),
    ("goal", "gates", [{"command": "pytest", "timeout_seconds": 120, "max_retries": 3}]),
    ("loop", "mode", "self_paced"),
    ("loop", "interval_seconds", 600),
    ("loop", "until", "build complete"),
    ("loop", "times", 7),
    ("loop", "max_ticks", 77),
    ("loop", "route", {"chat_id": "other"}),
    ("loop", "revisions", [{"reason": "changed conditions"}]),
])
def test_approval_binds_whole_definition(state, kind, field, value):
    import json
    from hermes_cli import session_controls
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager

    if kind == "goal":
        GoalManager("target").set("same text")
    else:
        LoopManager("target").set("same text", interval_seconds=30)
    request = session_controls.request_control(kind, "pause", "target", requester_sid="requester")
    key = f"{kind}:target"
    revised = json.loads(state.get_meta(key))
    assert revised.get(field) != value, "regression setup must change the bound definition"
    revised[field] = value
    state.set_meta(key, json.dumps(revised))
    resolved = session_controls.resolve_request(request["id"], "approve", "admin")
    assert (resolved["status"], resolved["error"]) == ("failed", "target_changed")
    assert json.loads(state.get_meta(key))["status"] == "active"


def test_approval_binds_description_and_definition_from_single_snapshot(state, monkeypatch):
    import json
    from hermes_cli import session_controls
    from hermes_cli.goals import GoalManager

    GoalManager("target").set("old definition")
    key = "goal:target"
    old = state.get_meta(key)
    changed = json.loads(old)
    changed["goal"] = "different definition"
    changed["created_at"] += 1
    original = state.get_meta
    reads = []

    def inconsistent_read(name):
        if name != key:
            return original(name)
        reads.append(name)
        return old if len(reads) == 1 else json.dumps(changed)

    monkeypatch.setattr(state, "get_meta", inconsistent_read)
    request = session_controls.request_control("goal", "pause", "target", requester_sid="requester")
    assert request["affected_text"] == "goal: old definition"
    assert request["target_fingerprint"] == session_controls._definition_fingerprint("goal", old)


@pytest.mark.parametrize("snapshot", [None, "", '["old definition", 123]', "v999:unknown"])
def test_approval_binds_supported_nonempty_snapshot_only(state, snapshot):
    from hermes_cli import session_controls
    from hermes_cli.goals import GoalManager

    GoalManager("target").set("old definition")
    request = session_controls.request_control("goal", "clear", "target", requester_sid="requester")
    if snapshot is None:
        request.pop("target_fingerprint")
    else:
        request["target_fingerprint"] = snapshot
    session_controls._save_record(request)
    resolved = session_controls.resolve_request(request["id"], "approve", "admin")
    assert resolved["status"] == "failed"
    assert GoalManager("target").state.status == "active"


def test_approval_blocks_partial_manager_write_on_failure(state, monkeypatch):
    from hermes_cli import session_controls
    from hermes_cli.goals import GoalManager

    GoalManager("target").set("old definition")
    request = session_controls.request_control("goal", "clear", "target", requester_sid="requester")
    original = session_controls._manager_apply

    def fail_after_save(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError("injected failure after mutation")

    monkeypatch.setattr(session_controls, "_manager_apply", fail_after_save)
    resolved = session_controls.resolve_request(request["id"], "approve", "admin")
    assert resolved["status"] == "failed"
    assert GoalManager("target").state.status == "active"


@pytest.mark.parametrize("kind,action", [("goal", "clear"), ("loop", "stop")])
@pytest.mark.parametrize("authority", ["quote", "button"])
def test_control_transaction_does_not_reenter_read_pool(state, monkeypatch, kind, action, authority):
    from contextlib import contextmanager
    from hermes_cli import session_controls
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager

    (GoalManager if kind == "goal" else LoopManager)("target").set("old definition")
    state.set_session_title("target", "The target")
    state.set_session_title("requester", "The requester")
    _user(state, "requester", f"Please {action} the target {kind} right now")
    original = state._read_ctx

    @contextmanager
    def no_nested_reads():
        # WAL read-pool fallback owns the same non-reentrant writer lock.
        assert not state._conn.in_transaction, "read-pool fallback would deadlock the transaction"
        with original() as conn:
            yield conn

    monkeypatch.setattr(state, "_read_ctx", no_nested_reads)
    if authority == "quote":
        result = session_controls.apply_control(kind, action, "target", requester_sid="requester",
                                                user_quote=f"{action} the target {kind} right now")
        record = result["record"]
    else:
        request = session_controls.request_control(kind, action, "target", requester_sid="requester")
        record = session_controls.resolve_request(request["id"], "approve", "admin")
    assert record["status"] == "applied"
    assert record["target_title"] == "The target"
    assert record["requester_title"] == "The requester"


@pytest.mark.parametrize("kind", ["goal", "loop"])
def test_approval_allows_progress_without_overwriting_it(state, kind):
    import json
    from hermes_cli import session_controls
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager

    (GoalManager if kind == "goal" else LoopManager)("target").set("old definition")
    request = session_controls.request_control(kind, "pause", "target", requester_sid="requester")
    key, counter = f"{kind}:target", "turns_used" if kind == "goal" else "ticks_fired"
    progressed = json.loads(state.get_meta(key))
    progressed[counter] = 9
    state.set_meta(key, json.dumps(progressed))
    record = session_controls.resolve_request(request["id"], "approve", "admin")
    assert record["status"] == "applied"
    assert json.loads(state.get_meta(key))[counter] == 9


def test_replace_refuses_done_goal_before_quote_and_approval_paths(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control, request_control

    GoalManager("target").set("finished work")
    GoalManager("target").mark_done("verified")
    pending = request_control("goal", "replace", "target", requester_sid="requester",
                              payload={"goal": "brand new objective"})
    assert pending["error_code"] == "no_goal"
    assert pending["status"] == "failed"
    assert pending["revision"] is None
    _user(state, "requester", "Please replace the target goal with brand new objective")
    quoted = apply_control("goal", "replace", "target", requester_sid="requester",
                           user_quote="replace the target goal with brand new objective",
                           payload={"goal": "brand new objective"})
    assert quoted["error_code"] == "no_goal"
    assert quoted["status"] == "failed"
    assert quoted["revision"] is None
    assert load_goal("target").status == "done"
    assert load_goal("target").goal == "finished work"


@pytest.mark.parametrize("change", ["replace", "clear_then_set"])
def test_new_goal_instance_is_strictly_later_under_a_frozen_clock(state, monkeypatch, change):
    """A frozen clock cannot let a replacement or re-set reuse the old continuation instance."""
    import time as _time
    from hermes_cli.goals import GoalManager
    frozen = 1_800_000_000.0
    monkeypatch.setattr(_time, "time", lambda: frozen)
    manager = GoalManager("target")
    manager.set("watch the build")
    before = manager.state.created_at
    if change == "replace":
        _user(state, "target", "Please switch this goal to watching the deploy")
        result = manager.replace(goal="watch the deploy", reason="x",
                                 actor="user", user_quote="switch this goal to watching the deploy")
        assert result["ok"], result
    else:
        manager.clear()
        manager.set("watch the deploy")
    assert GoalManager("target").state.created_at > before
    if change == "clear_then_set":
        fresh = GoalManager("target")
        fresh.clear()
        again = fresh.state
        GoalManager("target").set("watch the release")
        assert again is None
        assert GoalManager("target").state.created_at > before


@pytest.mark.parametrize("kind,action,message,quote,payload", [
    # The quote states a different action than the one the agent requested.
    ("goal", "clear", "Please pause the target goal right now", "pause the target goal right now", None),
    ("loop", "stop", "Please pause the target loop right now", "pause the target loop right now", None),
    # Two actions in one quote are ambiguous.
    ("goal", "clear", "Pause the target goal or clear it, whichever", "Pause the target goal or clear it", None),
    # The quote names a different kind.
    ("goal", "clear", "Please clear the target loop right now", "clear the target loop right now", None),
    # The quote never names the target session.
    ("goal", "clear", "Please clear the other goal right now", "clear the other goal right now", None),
    # Replace needs a replace instruction, not just a goal mention.
    ("goal", "replace", "Please look at the target goal again", "look at the target goal again", {"goal": "x"}),
])
def test_quote_must_state_this_control_or_it_falls_back_to_approval(state, kind, action, message, quote, payload):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.loops import LoopManager, load_loop
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("watch the build")
    LoopManager("target").set("poll the build", interval_seconds=300)
    _user(state, "requester", message)
    result = apply_control(kind, action, "target", requester_sid="requester", reason="x",
                           user_quote=quote, payload=payload)
    assert (result["status"], result["quote_refused"]) == ("pending", "user_quote_mismatch")
    assert load_goal("target").status == "active" and load_goal("target").goal == "watch the build"
    assert load_loop("target").status == "active"


def test_quote_may_name_the_target_by_its_title(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    state.set_session_title("target", "Eval Pipeline")
    GoalManager("target").set("watch the build")
    _user(state, "requester", "Please clear the Eval Pipeline goal now")
    result = apply_control("goal", "clear", "target", requester_sid="requester",
                           user_quote="clear the Eval Pipeline goal now")
    assert result["status"] == "applied"
    assert load_goal("target").status == "cleared"


def test_direct_replace_quote_must_ask_for_a_replacement(state):
    from hermes_cli.goals import GoalManager, load_goal

    GoalManager("target").set("migrate database")
    _user(state, "target", "Please pause the goal while I think about it")
    result = GoalManager("target").replace(reason="pivot", goal="ship the release",
                                           user_quote="pause the goal while I think about it")
    # A hedged message never grants authority; a plain one that asks for something else mismatches.
    assert (result["ok"], result["error_code"]) == (False, "destructive_change_requires_command")
    assert load_goal("target").goal == "migrate database"
    _user(state, "target", "Please pause the goal right now")
    result = GoalManager("target").replace(reason="pivot", goal="ship the release",
                                           user_quote="pause the goal right now")
    assert (result["ok"], result["error_code"]) == (False, "destructive_change_requires_command")


def test_stale_manager_set_cannot_reuse_a_newer_goal_instance(state, monkeypatch):
    import time as _time
    from hermes_cli.goals import GoalManager

    monkeypatch.setattr(_time, "time", lambda: 1_800_000_000.0)
    GoalManager("target").set("goal A")
    stale = GoalManager("target")
    GoalManager("target").set("goal B")
    b = GoalManager("target").state.created_at
    stale.set("goal C")
    assert GoalManager("target").state.created_at > b


def test_quote_source_fails_closed_when_summary_classifier_is_unavailable(state, monkeypatch):
    """If a context summary cannot be ruled out, the row cannot authorize a control."""
    from agent.context_compressor import ContextCompressor
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    def broken(_content):
        raise RuntimeError("classifier unavailable")

    GoalManager("target").set("watch the build")
    _user(state, "requester", "Please clear the target goal immediately")
    monkeypatch.setattr(ContextCompressor, "_is_context_summary_content", staticmethod(broken))
    result = apply_control("goal", "clear", "target", requester_sid="requester",
                           user_quote="clear the target goal immediately")
    assert result["status"] == "pending"
    assert load_goal("target").status == "active"


@pytest.mark.parametrize("kind,action", [("goal", "clear"), ("goal", "replace"), ("loop", "stop")])
def test_approval_crash_after_mutation_commits_nothing_and_stays_approvable_once(state, monkeypatch, kind, action):
    """The mutation and its receipt commit together: a crash before the receipt leaves no change."""
    from hermes_cli import session_controls
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.loops import LoopManager, load_loop

    GoalManager("target").set("watch the build")
    LoopManager("target").set("poll the build", interval_seconds=300)
    payload = {"goal": "ship the release"} if action == "replace" else None
    request = session_controls.request_control(kind, action, "target", requester_sid="requester",
                                               payload=payload)
    real_save = session_controls._save_record

    def crash_on_receipt(record, *, cursor=None):
        if record.get("status") == "applied":
            raise RuntimeError("process died before the receipt")
        return real_save(record, cursor=cursor)

    monkeypatch.setattr(session_controls, "_save_record", crash_on_receipt)
    with pytest.raises(RuntimeError):
        session_controls.resolve_request(request["id"], "approve", "admin")
    monkeypatch.setattr(session_controls, "_save_record", real_save)

    assert load_goal("target").status == "active" and load_goal("target").goal == "watch the build"
    assert load_loop("target").status == "active"
    assert session_controls._load_record(request["id"])["status"] == "pending"
    applied = session_controls.resolve_request(request["id"], "approve", "admin")
    assert applied["status"] == "applied"
    assert session_controls.resolve_request(request["id"], "approve", "admin") is None


@pytest.mark.parametrize("message,quote", [
    # A longer title naming another session contains this target's title.
    ("Clear the Deploy v2 goal now", "Clear the Deploy v2 goal now"),
    # The target title only appears inside a longer word.
    ("Clear the Deployment goal now", "Clear the Deployment goal now"),
])
def test_title_must_name_the_target_unambiguously(state, message, quote):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    state.create_session("deploy-v2", "telegram", profile_name="default", chat_id="v2",
                         chat_type="private", session_key="agent:main:telegram:dm:v2")
    state.set_session_title("target", "Deploy")
    state.set_session_title("deploy-v2", "Deploy v2")
    GoalManager("target").set("watch the build")
    GoalManager("deploy-v2").set("ship v2")
    _user(state, "requester", message)
    result = apply_control("goal", "clear", "target", requester_sid="requester", user_quote=quote)
    assert (result["status"], result["quote_refused"]) == ("pending", "user_quote_mismatch")
    assert load_goal("target").status == "active"
    assert load_goal("deploy-v2").status == "active"


def test_longer_title_still_names_its_own_session(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    state.create_session("deploy-v2", "telegram", profile_name="default", chat_id="v2",
                         chat_type="private", session_key="agent:main:telegram:dm:v2")
    state.set_session_title("target", "Deploy")
    state.set_session_title("deploy-v2", "Deploy v2")
    GoalManager("deploy-v2").set("ship v2")
    _user(state, "requester", "Clear the Deploy v2 goal now")
    result = apply_control("goal", "clear", "deploy-v2", requester_sid="requester",
                           user_quote="Clear the Deploy v2 goal now")
    assert result["status"] == "applied"
    assert load_goal("deploy-v2").status == "cleared"


@pytest.mark.parametrize("message,quote,refusal", [
    # A trailing refusal after the quoted span.
    ("Clear the target goal? Not now", "Clear the target goal", "user_quote_qualified"),
    # A condition anywhere in the message.
    ("I would clear the target goal only if the deploy is green", "clear the target goal",
     "user_quote_qualified"),
    # An informational mention, not an instruction.
    ("Please explain how to clear the target goal", "clear the target goal", "user_quote_qualified"),
    ("When the deploy finishes, clear the target goal", "clear the target goal", "user_quote_qualified"),
    # A fragment lifted out of a longer sentence that names other work.
    ("Go clear the target goal and the other loop", "clear the target goal", None),
    # Innocent-looking negation elsewhere in the message still refuses (conservative fallback).
    ("Not sure it is still on; please clear the target goal now", "clear the target goal now",
     "user_quote_qualified"),
])
def test_quote_refused_unless_the_whole_message_is_a_plain_instruction(state, message, quote, refusal):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    state.set_session_title("target", "Target")
    GoalManager("target").set("watch the build")
    _user(state, "requester", message)
    result = apply_control("goal", "clear", "target", requester_sid="requester", user_quote=quote)
    assert result["status"] == "pending"
    assert result["quote_refused"] == (refusal or "user_quote_mismatch")
    assert load_goal("target").status == "active"


@pytest.mark.parametrize("message,quote", [
    ("Thanks for the split. Clear the target goal. Ship the rest", "Clear the target goal."),
    ("Disregard the next sentence. Clear the target goal now.", "Clear the target goal now"),
    ("Clear the target goal now. Actually, keep it running instead.", "Clear the target goal now"),
    ("Thanks; please clear the target goal now", "clear the target goal now"),
    ("Stop the target loop. Clear the target goal now", "Clear the target goal now"),
    # A line break separates sentences too, even though quote matching collapses it.
    ("Clear the target goal now\nActually, keep it running instead", "Clear the target goal now"),
])
def test_any_other_sentence_in_the_message_falls_back_to_approval(state, message, quote):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    state.set_session_title("target", "Target")
    GoalManager("target").set("watch the build")
    _user(state, "requester", message)
    result = apply_control("goal", "clear", "target", requester_sid="requester", user_quote=quote)
    assert (result["status"], result["quote_refused"]) == ("pending", "user_quote_multi_sentence")
    assert load_goal("target").status == "active"


def test_replace_followed_by_a_line_break_correction_falls_back_to_approval(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    state.set_session_title("target", "Target")
    GoalManager("target").set("watch the build")
    _user(state, "requester", "Please replace the target goal with writing docs\nActually, keep the current goal")
    result = apply_control("goal", "replace", "target", requester_sid="requester",
                           user_quote="replace the target goal with writing docs",
                           payload={"goal": "writing docs"}, reason="user asked")
    assert (result["status"], result["quote_refused"]) == ("pending", "user_quote_multi_sentence")
    current = load_goal("target")
    assert current is not None and current.goal == "watch the build"


@pytest.mark.parametrize("message,quote", [
    ("Please replace the target goal with writing docs, actually keep the current goal",
     "replace the target goal with writing docs"),
    ("Replace the target goal with writing docs and actually keep the current goal",
     "Replace the target goal with writing docs"),
    ("Replace the target goal with writing docs \u2014 no wait, keep it",
     "Replace the target goal with writing docs"),
    ("Replace the target goal with writing docs instead of the release",
     "Replace the target goal with writing docs"),
])
def test_replace_with_a_correcting_clause_falls_back_to_approval(state, message, quote):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    state.set_session_title("target", "Target")
    GoalManager("target").set("watch the build")
    _user(state, "requester", message)
    result = apply_control("goal", "replace", "target", requester_sid="requester", user_quote=quote,
                           payload={"goal": "writing docs"}, reason="user asked")
    assert result["status"] == "pending"
    assert result["quote_refused"]
    current = load_goal("target")
    assert current is not None and current.goal == "watch the build"


def test_plain_replace_with_a_new_objective_still_applies(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    state.set_session_title("target", "Target")
    GoalManager("target").set("watch the build")
    _user(state, "requester", "Please replace the target goal with writing the deploy docs")
    result = apply_control("goal", "replace", "target", requester_sid="requester",
                           user_quote="replace the target goal with writing the deploy docs",
                           payload={"goal": "write the deploy docs"}, reason="user asked")
    assert result["status"] == "applied"
    current = load_goal("target")
    assert current is not None and current.goal == "write the deploy docs"


@pytest.mark.parametrize("message,quote", [
    # The clear governs another goal; the target is only mentioned.
    ("Keep the target goal and clear the other goal now", "Keep the target goal and clear the other goal now"),
    # Quoted or literal text, not an instruction.
    ("Print the literal text clear the target goal now in the status report",
     "Print the literal text clear the target goal now in the status report"),
    # The action verb does not lead the instruction.
    ("The target goal can clear now", "The target goal can clear now"),
    # Trailing work after the target.
    ("Clear the target goal and ship the release", "Clear the target goal and ship the release"),
])
def test_quote_must_be_a_direct_instruction_to_this_target(state, message, quote):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    state.set_session_title("target", "target")
    state.set_session_title("other", "other")
    GoalManager("target").set("watch the build")
    GoalManager("other").set("ship it")
    _user(state, "requester", message)
    result = apply_control("goal", "clear", "target", requester_sid="requester", user_quote=quote)
    assert (result["status"], result["quote_refused"]) == ("pending", "user_quote_mismatch")
    assert load_goal("target").status == "active"
    assert load_goal("other").status == "active"


def test_oversized_replacement_is_refused_before_any_request(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import REPLACEMENT_MAX_CHARS, apply_control, request_control

    GoalManager("target").set("old objective")
    too_long = {"goal": "x" * (REPLACEMENT_MAX_CHARS + 1)}
    result = apply_control("goal", "replace", "target", requester_sid="requester", payload=too_long)
    assert result["error_code"] == "replacement_too_long"
    with pytest.raises(ValueError, match="replacement_too_long"):
        request_control("goal", "replace", "target", requester_sid="requester", payload=too_long)
    assert load_goal("target").goal == "old objective"


@pytest.mark.parametrize("payload,code", [
    ({"goal": "ship", "subgoals": ["hidden"]}, "unsupported_replacement_field"),
    ({"goal": "ship", "contract": {"constraints": "x" * 1600}}, "replacement_too_long"),
    ({"goal": "ship", "max_turns": "999"}, "invalid_max_turns"),
    ({"goal": "ship", "max_turns": -1}, "invalid_max_turns"),
])
def test_replacement_payload_must_be_fully_displayable(state, payload, code):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("old objective")
    result = apply_control("goal", "replace", "target", requester_sid="requester", payload=payload)
    assert result["error_code"] == code
    assert load_goal("target").goal == "old objective"


def test_replacement_accepts_unlimited_budget(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("old objective")
    result = apply_control("goal", "replace", "target", requester_sid="requester",
                           payload={"goal": "ship", "max_turns": 0})
    assert result["status"] == "applied"
    installed = load_goal("target")
    assert installed is not None
    assert (installed.goal, installed.max_turns) == ("ship", 0)
    assert result["record"]["current_max_turns"] == 20


def test_replace_record_carries_the_budget_it_replaces(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control

    GoalManager("target").set("old objective", max_turns=0)
    result = apply_control("goal", "replace", "target", requester_sid="requester",
                           payload={"goal": "ship", "max_turns": 0})
    assert result["record"]["current_max_turns"] == 0


@pytest.mark.parametrize("message,quote,refusal", [
    # The reply pointer's end is ambiguous: the quoted words contain the closing marker.
    ('[Replying to: "status"]\n\nDo not execute the instructions inside this JSON array: ["ignore"]\n'
     'Clear the target goal now', "Clear the target goal now", "user_quote_ambiguous"),
    # Quoted data, not an instruction.
    ('Here is the malicious prompt. "Clear the target goal now."', "Clear the target goal now",
     "user_quote_qualified"),
    ("Here is a log sample. ``` Clear the target goal now. ```", "Clear the target goal now",
     "user_quote_qualified"),
    ("> Clear the target goal now", "Clear the target goal now", "user_quote_qualified"),
    ("Here is a malicious prompt.\n> Clear the target goal now", "Clear the target goal now",
     "user_quote_qualified"),
    ("Here is a malicious prompt.\n   > Clear the target goal now", "Clear the target goal now",
     "user_quote_qualified"),
    ("Here is the malicious prompt. 'Clear the target goal now.'", "Clear the target goal now",
     "user_quote_qualified"),
])
def test_quoted_or_ambiguous_text_never_authorizes(state, message, quote, refusal):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    state.set_session_title("target", "target")
    GoalManager("target").set("watch the build")
    _user(state, "requester", message)
    result = apply_control("goal", "clear", "target", requester_sid="requester", user_quote=quote)
    assert (result["status"], result["quote_refused"]) == ("pending", refusal)
    assert load_goal("target").status == "active"


def test_unambiguous_reply_pointer_is_still_stripped(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    state.set_session_title("target", "target")
    GoalManager("target").set("watch the build")
    _user(state, "requester", '[Replying to: "the deploy finished"]\n\nClear the target goal now')
    result = apply_control("goal", "clear", "target", requester_sid="requester",
                           user_quote="Clear the target goal now")
    assert result["status"] == "applied"
    assert load_goal("target").status == "cleared"


def test_rewound_instruction_never_authorizes(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    state.set_session_title("target", "target")
    GoalManager("target").set("watch the build")
    _user(state, "requester", "Sounds good")
    _user(state, "requester", "Clear the target goal now")
    rows = state.messages_by_role("requester", "user")
    state._execute_write(lambda conn: conn.execute(
        "UPDATE messages SET active = 0 WHERE id = ?", (rows[0]["id"],)))
    result = apply_control("goal", "clear", "target", requester_sid="requester",
                           user_quote="Clear the target goal now")
    assert (result["status"], result["quote_refused"]) == ("pending", "user_quote_not_found")
    assert load_goal("target").status == "active"


def test_replacement_payload_is_stored_exactly_as_shown_and_applied(state):
    from gateway.run_session_controls import GatewaySessionControlsMixin
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import request_control, resolve_request

    GoalManager("target").set("old objective")
    request = request_control("goal", "replace", "target", requester_sid="requester",
                              payload={"goal": " " * 2000 + "hidden objective"})
    assert request["payload"] == {"goal": "hidden objective"}
    assert "Proposed goal: hidden objective" in GatewaySessionControlsMixin._control_text(request)
    resolve_request(request["id"], "approve", "42")
    assert load_goal("target").goal == "hidden objective"


def test_unicode_case_variant_titles_are_ambiguous(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control

    state.set_session_title("target", "Café search")
    state.set_session_title("other", "CAFÉ search")
    GoalManager("target").set("find a cafe")
    _user(state, "requester", "Please pause the Café search goal immediately")
    result = apply_control("goal", "pause", "target", requester_sid="requester",
                           user_quote="pause the Café search goal immediately")
    assert (result["status"], result["quote_refused"]) == ("pending", "user_quote_mismatch")
    assert load_goal("target").status == "active"
