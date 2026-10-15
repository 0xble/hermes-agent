"""Acceptance tests for the goal redesign slices 1-5 and 7."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _manager(sid="redesign"):
    from hermes_cli.goals import GoalManager
    mgr = GoalManager(sid, default_max_turns=0)
    mgr.set("finish the long-running verification")
    return mgr


def test_slice_1_uncapped_seconds_and_wait_until_persist(hermes_home):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager, _parse_judge_response

    v, reason, failed, directive = _parse_judge_response(
        '{"verdict":"wait","wait_for_seconds":604800,"reason":"Sunday window"}'
    )
    assert (v, failed, directive) == ("wait", False, {"seconds": 604800})
    mgr = _manager("slice-1-seconds")
    with patch.object(goals, "judge_goal", return_value=(v, reason, failed, directive, False)):
        mgr.evaluate_after_turn("Nothing to do until Sunday.")
    assert mgr.state.waiting_seconds == 604800

    iso = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    v, _, _, directive = _parse_judge_response(
        json.dumps({"verdict": "wait", "wait_until": iso, "reason": "scheduled check"})
    )
    assert v == "wait" and directive["until"] > 0


def test_slice_2_event_rejudges_waiting_goal_once(hermes_home):
    from hermes_cli import goals
    mgr = _manager("slice-2-event")
    until = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    with patch.object(goals, "judge_goal", return_value=("wait", "scheduled", False, {"until": until}, False)):
        mgr.evaluate_after_turn("Waiting for the scheduled event.")
    calls = []
    with patch.object(goals, "judge_goal", return_value=("continue", "event received", False, None, False)) as judge:
        decision = mgr.evaluate_after_turn("Cron delivered the result.", user_initiated=False, external_event=True)
        calls.append(judge.call_count)
    assert calls == [1]
    assert decision["verdict"] == "continue"
    assert mgr.state.waiting_until == 0


def test_slice_3_no_progress_ladder_stays_active_and_quiet(hermes_home):
    from hermes_cli import goals
    mgr = _manager("slice-3-ladder")
    waits = []
    decisions = []
    with patch.object(goals, "judge_goal", return_value=("continue", "still waiting", False, None, False)):
        for i in range(10):
            d = mgr.evaluate_after_turn("", user_initiated=False)
            decisions.append(d)
            waits.append(mgr.state.waiting_seconds)
            # Simulate the idle wake reaching the next ladder rung.
            mgr.state.clear_wait()
            mgr._save()
    assert mgr.state.status == "active"
    assert waits[:4] == [900, 3600, 14400, 86400]
    assert all(not d.get("message") for d in decisions)


def test_slice_4_judge_outage_stays_active_with_one_notice(hermes_home):
    from hermes_cli import goals
    mgr = _manager("slice-4-outage")
    with patch.object(goals, "judge_goal", return_value=("continue", "HTTP 500", False, None, True)):
        decisions = [mgr.evaluate_after_turn("working") for _ in range(10)]
    assert all(d["status"] == "active" for d in decisions)
    assert all(d["verdict"] == "continue" for d in decisions)
    assert sum(bool(d["message"]) for d in decisions) == 1


def test_slice_5_brian_block_is_user_message_wait_and_real_breach_pauses(hermes_home):
    from hermes_cli import goals
    mgr = _manager("slice-5-brian")
    with patch.object(goals, "judge_goal", return_value=("blocked", "Waiting for Brian to approve the Sunday window", False, None, False)):
        decision = mgr.evaluate_after_turn("I need Brian's decision.")
    assert decision["status"] == "active"
    assert mgr.state.waiting_on_user_message is True

    mgr = _manager("slice-5-breach")
    with patch.object(goals, "judge_goal", return_value=("blocked", "The stated irreversible constraint makes this impossible", False, None, False)):
        decision = mgr.evaluate_after_turn("The constraint is breached.")
    assert decision["status"] == "paused"


def test_slice_7_state_has_no_removed_breaker_fields(hermes_home):
    from hermes_cli.goals import GoalState
    state = GoalState.from_json(json.dumps({"goal": "x", "status": "active"}))
    encoded = json.loads(state.to_json())
    for field in ("consecutive_silent_no_progress", "barrier_rearms", "barrier_recheck_at", "last_age_notice_key", "consecutive_disputes", "last_dispute_evidence"):
        assert field not in encoded
    assert "waiting_on_user_message" in encoded
