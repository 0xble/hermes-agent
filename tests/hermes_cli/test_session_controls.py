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
    assert (stale["status"], stale["quote_refused"]) == ("pending", "user_quote_not_found")
    assert (paraphrase["status"], paraphrase["quote_refused"]) == ("pending", "user_quote_not_found")
    assert GoalManager("target").state.status == "active"


def test_quote_from_different_session_and_relay_are_refused(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control
    GoalManager("target").set("watch the build")
    _user(state, "other", "Please clear the target goal immediately")
    different = apply_control("goal", "clear", "target", requester_sid="requester",
                              reason="x", user_quote="clear the target goal immediately")
    assert (different["status"], different["quote_refused"]) == ("pending", "user_quote_not_found")
    _user(state, "requester", "[relay from=other receipt=abc]\nPlease clear the target goal immediately")
    relay = apply_control("goal", "clear", "target", requester_sid="requester",
                          reason="x", user_quote="Please clear the target goal immediately")
    assert (relay["status"], relay["quote_refused"]) == ("pending", "user_quote_not_found")


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
    # A card without a target snapshot is never approval authority, even for a no-op.
    assert resolved["error"] == "target_changed"
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
    assert (result["status"], result["quote_refused"]) == ("pending", "user_quote_negated")
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


def test_approval_is_bound_to_the_definition_on_the_card(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.loops import LoopManager, load_loop
    from hermes_cli.session_controls import request_control, resolve_request

    GoalManager("target").set("old objective")
    request = request_control("goal", "clear", "target", requester_sid="requester")
    GoalManager("target").clear()
    GoalManager("target").set("a different objective")
    resolved = resolve_request(request["id"], "approve", "admin")
    assert resolved["status"] == "failed"
    assert resolved["error"] == "target_changed"
    assert load_goal("target").goal == "a different objective"
    assert load_goal("target").status == "active"
    assert resolve_request(request["id"], "approve", "admin") is None

    LoopManager("target").set("check the build")
    loop_request = request_control("loop", "stop", "target", requester_sid="requester")
    LoopManager("target").clear()
    LoopManager("target").set("check a different thing")
    stopped = resolve_request(loop_request["id"], "approve", "admin")
    assert stopped["error"] == "target_changed"
    assert load_loop("target").prompt == "check a different thing"

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
            capture_output=True, text=True, timeout=10,
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


def test_replace_refuses_done_goal_on_quote_and_approval_paths(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control, request_control, resolve_request

    GoalManager("target").set("finished work")
    GoalManager("target").mark_done("verified")
    pending = request_control("goal", "replace", "target", requester_sid="requester",
                              payload={"goal": "brand new objective"})
    approved = resolve_request(pending["id"], "approve", "admin")
    assert approved["status"] == "failed"
    _user(state, "requester", "Please replace the goal with brand new objective")
    quoted = apply_control("goal", "replace", "target", requester_sid="requester",
                           user_quote="replace the goal with brand new objective",
                           payload={"goal": "brand new objective"})
    assert quoted["ok"] is False
    assert load_goal("target").status == "done"
    assert load_goal("target").goal == "finished work"
