"""Targeted loop edits use the existing autonomous session-control transaction."""
from __future__ import annotations

import time

import pytest

# User-message persistence reaches the runtime bootstrap. Load it before the per-test
# real-home guard, as the application entry point does.
import hermes_bootstrap  # noqa: F401

from hermes_cli import goals, loops, session_controls


def load_loop(sid):
    state_ = loops.load_loop(sid)
    assert state_ is not None, f"no loop for {sid}"
    return state_


def _config(home, *, goals_on=True, loops_on=True):
    lines = []
    if goals_on:
        lines.append("goals:\n  autonomy: true\n")
    if loops_on:
        lines.append("loops:\n  autonomy: true\n  max_ticks: 50\n")
    (home / "config.yaml").write_text("".join(lines), encoding="utf-8")


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    from hermes_state_registry import acquire, release_or_close
    db = acquire(home / "state.db")
    for sid in ("requester", "target", "other"):
        db.create_session(sid, "telegram", profile_name="default", chat_id=sid,
                          chat_type="private", session_key=f"agent:main:telegram:dm:{sid}")
    goals._DB_CACHE[str(home)] = db
    _config(home)
    yield db
    goals._DB_CACHE.clear()
    release_or_close(db)


def _home(db):
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _loop(sid="target", **kwargs):
    manager = loops.LoopManager(sid)
    manager.set("watch the deploy", **{"interval_seconds": 300, "times": 5, "until": "deploy green", **kwargs})
    return manager


def _revise(payload, *, target="target", requester="requester", reason="owner decided", **kwargs):
    return session_controls.apply_control("loop", "revise", target, requester_sid=requester,
                                          reason=reason, payload=payload, **kwargs)


# --- loop revise -------------------------------------------------------------------------

def test_cross_session_revise_applies_cadence_and_prompt_under_autonomy_without_a_card(state):
    _loop()
    before = load_loop("target")
    result = _revise({"prompt": "watch the canary", "interval_seconds": 600})
    assert result["ok"] and result["status"] == "applied", result
    after = load_loop("target")
    assert after.prompt == "watch the canary"
    assert after.interval_seconds == 600 and after.mode == "interval"
    # The changed cadence reschedules the target's next wake from the new delay.
    assert after.next_due_at - time.time() > 590
    assert after.next_due_at > before.next_due_at
    revision = after.revisions[-1]
    assert revision["authority"] == "autonomy" and revision["actor"] == "agent"
    assert revision["after"]["interval_seconds"] == 600 and revision["after"]["prompt"] == "watch the canary"
    record = session_controls._load_record(result["record"]["id"])
    assert record["status"] == "applied" and record["authority"]["via"] == "autonomy"
    assert record["authority"]["session_id"] == "requester"
    assert record["loop_schedule"]["instance"] == after.created_at
    assert record["loop_schedule"]["version"] == after.version
    assert record["loop_schedule"]["next_due_at"] == after.next_due_at
    assert not [r for r in session_controls.pending_outbox() if r.get("status") == "pending"]
    assert "revised" in result["notice"]


def test_revise_switches_the_target_to_self_paced(state):
    _loop()
    result = _revise({"self_paced": True})
    assert result["ok"], result
    assert load_loop("target").mode == "self_paced"


def test_revise_receipts_are_bound_to_the_schedule_committed_by_each_write(state):
    _loop()
    first = _revise({"interval_seconds": 600})
    version_after_first = load_loop("target").version
    second = _revise({"prompt": "watch another thing"})
    assert first["ok"] and second["ok"]
    after = load_loop("target")
    assert after.version > version_after_first
    assert [r["after"].get("prompt") for r in after.revisions][-1] == "watch another thing"
    assert len(after.revisions) == 2
    assert first["record"]["loop_schedule"]["version"] == version_after_first
    assert second["record"]["loop_schedule"]["version"] == after.version


def test_revise_with_nothing_to_change_reports_no_change(state):
    _loop()
    result = _revise({"interval_seconds": 300})
    assert not result["ok"] and result["error_code"] == "no_change"


def test_revise_without_loop_autonomy_is_refused_and_changes_nothing(state):
    _config(_home(state), loops_on=False)
    _loop()
    result = _revise({"interval_seconds": 600})
    assert not result["ok"] and result["error_code"] == "autonomy_required"
    assert load_loop("target").interval_seconds == 300
    assert not [r for r in session_controls.pending_outbox() if r.get("status") == "pending"]
    requested = session_controls.request_control("loop", "revise", "target", requester_sid="requester",
                                                 reason="x", payload={"interval_seconds": 600})
    assert requested["error_code"] == "autonomy_required"


@pytest.mark.parametrize("target,error", [("missing-session", "unknown_target"), ("other", "no_loop")])
def test_revise_a_missing_target_or_non_loop_session_fails_cleanly(state, target, error):
    result = _revise({"interval_seconds": 600}, target=target)
    assert not result["ok"] and result["error_code"] == error, result


@pytest.mark.parametrize("payload,error", [
    ({"surprise": 1}, "unsupported_loop_field"),
    ({"interval_seconds": 0}, "invalid_loop_interval_seconds"),
    ({"times": -1}, "invalid_loop_times"),
    ({"self_paced": True, "interval_seconds": 600}, "invalid_loop_cadence"),
])
def test_revise_payload_is_validated_before_anything_changes(state, payload, error):
    _loop()
    result = _revise(payload)
    assert not result["ok"] and result["error_code"] == error
    assert load_loop("target").revisions == []


# --- loop replace ------------------------------------------------------------------------

def test_cross_session_replace_applies_under_autonomy_and_records_the_revision(state):
    _loop()
    result = session_controls.apply_control(
        "loop", "replace", "target", requester_sid="requester", reason="new orders",
        payload={"prompt": "watch the canary", "interval_seconds": 900, "times": 3, "until": "canary green"})
    assert result["ok"] and result["status"] == "applied", result
    after = load_loop("target")
    assert (after.prompt, after.interval_seconds, after.until) == ("watch the canary", 900, "canary green")
    assert after.revisions[-1]["kind"] == "replace" and after.revisions[-1]["authority"] == "autonomy"
    assert after.revisions[-1]["actor"] == "agent"
    assert result["record"]["loop_schedule"]["version"] == after.version
    assert "replaced" in result["notice"]


def test_replace_requires_a_prompt_and_autonomy(state):
    _loop()
    missing = session_controls.apply_control("loop", "replace", "target", requester_sid="requester",
                                             reason="x", payload={"interval_seconds": 900})
    assert missing["error_code"] == "replacement_prompt_required"
    _config(_home(state), loops_on=False)
    refused = session_controls.apply_control("loop", "replace", "target", requester_sid="requester",
                                             reason="x", payload={"prompt": "other"})
    assert refused["error_code"] == "autonomy_required"
    assert load_loop("target").prompt == "watch the deploy"


# --- loop pause/resume/stop and clear stay as they were ---------------------------------

def test_edit_requires_a_reason_and_cannot_claim_quote_authority(state):
    _loop()
    assert _revise({"interval_seconds": 600}, reason=" ")["error_code"] == "reason_required"
    result = _revise({"interval_seconds": 600}, user_quote="Revise the target loop")
    assert result["ok"], result
    assert result["record"]["authority"]["via"] == "autonomy"
    assert result["record"]["authority"]["quote"] == ""


def test_pause_resume_and_stop_are_unchanged(state):
    _loop()
    paused = session_controls.apply_control("loop", "pause", "target", requester_sid="requester", reason="hold")
    assert paused["ok"] and load_loop("target").status == "paused"
    resumed = session_controls.apply_control("loop", "resume", "target", requester_sid="requester", reason="go")
    assert resumed["ok"] and load_loop("target").status == "active"
    stopped = session_controls.apply_control("loop", "stop", "target", requester_sid="requester", reason="done")
    assert stopped["ok"]
    # The agent finishes the loop; it never clears it.
    assert load_loop("target").status != "cleared"


def test_loop_stop_without_autonomy_still_needs_the_users_words(state):
    _config(_home(state), loops_on=False)
    _loop()
    result = session_controls.apply_control("loop", "stop", "target", requester_sid="requester", reason="done")
    assert result.get("status") != "applied"
    assert load_loop("target").status == "active"
