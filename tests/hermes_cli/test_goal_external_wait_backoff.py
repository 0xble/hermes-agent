"""Regression tests for external-wait parking and no-progress backoff."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def test_judge_prompt_allows_external_scheduled_wait(hermes_home):
    """The judge must be told that outside services, cron, and elapsed holds are waitable."""
    from hermes_cli import goals

    captured = {}

    class _Msg:
        content = '{"verdict": "wait", "wait_for_seconds": 900, "reason": "cron check at 17:45Z"}'

    class _Choice:
        message = _Msg()

    class _Response:
        choices = [_Choice()]

    def call(**kwargs):
        captured.update(kwargs)
        return _Response()

    with patch("agent.auxiliary_client.call_llm", side_effect=call):
        verdict, reason, parse_failed, directive, _transport_failed = goals.judge_goal(
            "wait for the external consolidation service and report its next watchdog result",
            "Nothing actionable now; the external service is still draining and the cron watchdog runs at 17:45Z.",
        )

    prompt = "\n".join(m["content"] for m in captured["messages"])
    assert "external service" in prompt.lower()
    assert "cron" in prompt.lower()
    assert "scheduled" in prompt.lower()
    assert verdict == "wait"
    assert reason == "cron check at 17:45Z"
    assert parse_failed is False
    assert directive == {"seconds": 900}


def test_timed_wait_is_bounded(hermes_home):
    """A model cannot park a goal beyond the existing bounded barrier window."""
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("external-wait-cap")
    mgr.set("wait for the external service", max_turns=10)
    with patch.object(
        goals,
        "judge_goal",
        return_value=("wait", "external service next check", False, {"seconds": 999999}, False),
    ):
        decision = mgr.evaluate_after_turn(
            "Nothing actionable until the external service finishes; wait for the next scheduled check.",
            user_initiated=False,
        )

    assert decision["verdict"] == "wait"
    assert decision["should_continue"] is False
    assert mgr.state.waiting_until - mgr.state.waiting_since <= goals._MAX_BARRIER_WAIT_S + 1
    assert mgr.state.waiting_until > mgr.state.waiting_since


def test_live_session_barrier_rearms_after_age_cap(hermes_home):
    """A watcher session that remains live must keep its trigger barrier armed."""
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("session-rearm")
    mgr.set("wait for watcher signal")
    mgr.wait_on_session("watcher-1", reason="watcher")
    state = mgr.state
    assert state is not None
    state.waiting_since = time.time() - goals._MAX_BARRIER_WAIT_S - 1
    state.barrier_recheck_at = 0.0
    mgr._save()
    with patch.object(goals, "_session_waiting", return_value=True):
        mgr.rearm_live_barrier()
        assert mgr.is_waiting() is True
    assert mgr.state.waiting_on_session == "watcher-1"
    assert mgr.state.barrier_recheck_at > time.time()
    assert mgr.state.waiting_until == 0.0
    assert mgr.state.barrier_rearms == 1


def test_user_turn_at_age_cap_stages_notice_without_conflict(hermes_home):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("age-user-turn")
    mgr.set("wait for watcher")
    mgr.wait_on_session("watcher-user", reason="external watcher")
    mgr.state.waiting_since = time.time() - goals._MAX_BARRIER_WAIT_S - 1
    mgr.state.barrier_recheck_at = 0.0
    mgr._save()
    with patch.object(goals, "_session_waiting", return_value=True), patch.object(goals, "judge_goal") as judge:
        decision = mgr.evaluate_after_turn("user checked the status", user_initiated=True)

    assert decision["verdict"] == "waiting"
    assert "30 minutes" in decision["message"]
    assert mgr.state.turns_used == 0
    assert mgr.state.last_age_notice_key.startswith("live-barrier:session watcher-user")
    judge.assert_not_called()


def test_user_turn_at_hard_cap_pauses_without_judge(hermes_home):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("hard-cap-user-turn")
    mgr.set("wait for watcher")
    mgr.wait_on_session("watcher-hard-user", reason="external watcher")
    mgr.state.waiting_since = time.time() - goals._MAX_LIVE_BARRIER_S - 1
    mgr.state.barrier_recheck_at = 0.0
    mgr._save()
    with patch.object(goals, "_session_waiting", return_value=True), patch.object(goals, "judge_goal") as judge:
        decision = mgr.evaluate_after_turn("user checked the status", user_initiated=True)

    assert decision["status"] == "paused"
    assert "watcher-hard-user" in decision["message"]
    assert mgr.state.turns_used == 0
    judge.assert_not_called()


def test_age_notice_does_not_repost_parked_notice(hermes_home):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("age-notice-dedupe")
    mgr.set("wait for watcher")
    mgr.wait_on_session("watcher-dedupe", reason="external watcher")
    with patch.object(goals, "_session_waiting", return_value=True):
        first = mgr.evaluate_after_turn("internal status", user_initiated=False)
        assert "Goal parked" in first["message"]
        mgr.state.waiting_since = time.time() - goals._MAX_BARRIER_WAIT_S - 1
        mgr.state.barrier_recheck_at = 0.0
        mgr._save()
        assert mgr.rearm_live_barrier()
        second = mgr.evaluate_after_turn("internal status", user_initiated=False)

    assert second["message"] == ""
    assert mgr.state.last_wait_notice_key == "session:watcher-dedupe|reason:external watcher"
    assert mgr.state.last_age_notice_key.startswith("live-barrier:session watcher-dedupe")


def test_read_only_status_turns_back_off_with_varied_results(hermes_home):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("varied-status-backoff", default_max_turns=20)
    mgr.set("wait for external consolidation", max_turns=20)
    rows = [
        [{"tool": "terminal", "call": '{"command": "gh run view 1"}', "output": "queued", "timestamp": time.time() + 1}],
        [{"tool": "terminal", "call": '{"command": "gh run view 1"}', "output": "in_progress", "timestamp": time.time() + 2}],
        [{"tool": "terminal", "call": '{"command": "gh run view 1"}', "output": "completed", "timestamp": time.time() + 3}],
    ]
    with patch.object(goals, "collect_goal_evidence", side_effect=rows), patch.object(
        goals, "judge_goal", side_effect=[
            ("continue", "still checking", False, None, False),
            ("continue", "watchdog has not finished", False, None, False),
            ("continue", "waiting for the next poll", False, None, False),
        ]
    ):
        decisions = [mgr.evaluate_after_turn("status only", user_initiated=False) for _ in rows]

    assert [d["verdict"] for d in decisions] == ["continue", "continue", "wait"]
    assert mgr.state.consecutive_no_progress == goals.DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS


def test_read_only_status_regex_rejects_redirects_and_pipes():
    from hermes_cli import goals

    assert goals._READ_ONLY_STATUS_CALL_RE.match("gh run view 1")
    assert not goals._READ_ONLY_STATUS_CALL_RE.match("cat a > b")
    assert not goals._READ_ONLY_STATUS_CALL_RE.match("gh run view 1 | tee status")


def test_three_qualifying_no_progress_turns_back_off_and_persist(hermes_home):
    """Repeated no-tool, same-reason continuations park instead of busy-polling."""
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("no-progress-backoff", default_max_turns=20)
    mgr.set("wait for external consolidation", max_turns=20)
    with patch.object(goals, "collect_goal_evidence", return_value=[]), patch.object(
        goals, "judge_goal", return_value=("continue", "nothing actionable until the watchdog", False, None, False)
    ):
        d1 = mgr.evaluate_after_turn("Nothing to do until the watchdog runs.", user_initiated=False)
        d2 = mgr.evaluate_after_turn("Nothing to do until the watchdog runs.", user_initiated=False)
        d3 = mgr.evaluate_after_turn("Nothing to do until the watchdog runs.", user_initiated=False)

    assert d1["should_continue"] is True
    assert d2["should_continue"] is True
    assert d3["verdict"] == "wait"
    assert d3["should_continue"] is False
    assert mgr.state.consecutive_no_progress == goals.DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS
    assert mgr.state.waiting_until > mgr.state.waiting_since

    restored = GoalManager("no-progress-backoff")
    assert restored.state.consecutive_no_progress == goals.DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS
    assert restored.is_waiting()


def test_live_delegation_parks_an_unproductive_automatic_turn(hermes_home):
    """An active delegation is a deterministic wake source even when the judge says CONTINUE."""
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("delegation-no-progress", default_max_turns=20)
    mgr.set("wait for delegated PR review", max_turns=20)
    with patch.object(goals, "collect_goal_evidence", return_value=[]), patch.object(
        goals, "judge_goal", return_value=("continue", "the delegated review is still pending", False, None, False)
    ):
        decision = mgr.evaluate_after_turn(
            "The delegated review is still pending; there is no local action to take.",
            user_initiated=False,
            active_delegations=1,
        )

    assert decision["verdict"] == "wait"
    assert decision["should_continue"] is False
    assert "active delegation" in decision["message"]
    assert mgr.state is not None
    assert mgr.state.waiting_on_delegations == 1
    assert mgr.state.waiting_until > mgr.state.waiting_since
    with patch.object(goals, "count_active_delegations", return_value=0):
        assert mgr.is_waiting() is False


def test_read_only_status_checks_with_live_delegation_also_park(hermes_home):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("delegation-status-only", default_max_turns=20)
    mgr.set("wait for delegated PR review", max_turns=20)
    evidence = [{"tool": "terminal", "call": '{"command": "gh pr checks 302"}', "output": "pending", "timestamp": 1.0}]
    with patch.object(goals, "collect_goal_evidence", return_value=evidence), patch.object(
        goals, "judge_goal", return_value=("continue", "the delegated review is still pending", False, None, False)
    ):
        decision = mgr.evaluate_after_turn(
            "The delegated review is still pending; I only checked its status.",
            user_initiated=False,
            active_delegations=1,
        )

    assert decision["verdict"] == "wait"
    assert mgr.state is not None
    assert mgr.state.waiting_on_delegations == 1


def test_progress_and_real_user_turn_reset_no_progress_streak(hermes_home):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("no-progress-reset", default_max_turns=20)
    mgr.set("wait for external consolidation", max_turns=20)
    evidence = [
        {"tool": "terminal", "call": "echo progress", "output": "progress", "timestamp": time.time() + 60},
    ]
    with patch.object(
        goals, "collect_goal_evidence", side_effect=[[], [], evidence, evidence, evidence, evidence]
    ), patch.object(
        goals, "judge_goal", return_value=("continue", "nothing actionable until the watchdog", False, None, False)
    ):
        mgr.evaluate_after_turn("Nothing to do until the watchdog runs.", user_initiated=False)
        mgr.evaluate_after_turn("Nothing to do until the watchdog runs.", user_initiated=False)
        # New recorded tool evidence is deterministic progress and resets the streak.
        d3 = mgr.evaluate_after_turn("The service status is unchanged, but I checked it.", user_initiated=False)
        assert d3["verdict"] == "continue"
        assert mgr.state.consecutive_no_progress == 0
        # A real user turn also resets it, even if the judge reason repeats.
        mgr.evaluate_after_turn("User supplied a clarification.", user_initiated=True)
        assert mgr.state.consecutive_no_progress == 0


def test_unchanged_continuation_reason_is_not_reposted(hermes_home):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("continuation-notice-dedupe", default_max_turns=20)
    mgr.set("wait for external consolidation", max_turns=20)
    with patch.object(goals, "collect_goal_evidence", return_value=[]), patch.object(
        goals, "judge_goal", return_value=("continue", "nothing actionable until the watchdog", False, None, False)
    ):
        first = mgr.evaluate_after_turn("Nothing to do until the watchdog.", user_initiated=False)
        second = mgr.evaluate_after_turn("Still nothing to do until the watchdog.", user_initiated=False)

    assert "Continuing toward goal" in first["message"]
    assert second["message"] == ""
    assert mgr.state is not None
    assert mgr.state.last_continuation_notice_key == "continuation|reason:nothing actionable until the watchdog"


def test_live_barrier_emits_one_age_notice(hermes_home):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("age-notice")
    mgr.set("wait for watcher")
    mgr.wait_on_session("watcher-age", reason="external watcher")
    mgr.state.waiting_since = time.time() - goals._MAX_BARRIER_WAIT_S - 1
    mgr.state.barrier_recheck_at = 0.0
    mgr._save()
    with patch.object(goals, "_session_waiting", return_value=True):
        first = mgr.rearm_live_barrier()
        second = mgr.rearm_live_barrier()

    assert first and "30 minutes" in first
    assert second is None
    assert mgr.state.last_age_notice_key.startswith("live-barrier:session watcher-age")
    assert mgr.state.waiting_until == 0.0


def test_live_barrier_pauses_at_hard_ceiling(hermes_home):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("hard-cap")
    mgr.set("wait for watcher")
    mgr.wait_on_session("watcher-hard-cap", reason="external watcher")
    mgr.state.waiting_since = time.time() - goals._MAX_LIVE_BARRIER_S - 1
    mgr.state.barrier_recheck_at = 0.0
    mgr._save()
    with patch.object(goals, "_session_waiting", return_value=True):
        notice = mgr.rearm_live_barrier()

    assert notice and "watcher-hard-cap" in notice and "6h" in notice
    assert mgr.state.status == "paused"
    assert "watcher-hard-cap" in mgr.state.paused_reason


def test_live_barrier_rearm_respects_cas_loss(hermes_home, monkeypatch):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager("rearm-cas")
    mgr.set("wait for watcher")
    mgr.wait_on_session("watcher-cas", reason="external watcher")
    since = mgr.state.waiting_since
    mgr.state.waiting_since = time.time() - goals._MAX_BARRIER_WAIT_S - 1
    mgr.state.barrier_recheck_at = 0.0
    mgr._save()
    since = mgr.state.waiting_since
    concurrent = goals.load_goal("rearm-cas")
    concurrent.waiting_on_session = "newer-watcher"
    concurrent.waiting_since = since + 10
    goals.save_goal("rearm-cas", concurrent)
    monkeypatch.setattr(goals, "_session_waiting", lambda _sid: True)

    assert mgr.rearm_live_barrier() is None
    assert mgr.state.waiting_on_session == "newer-watcher"
    assert mgr.state.waiting_since == since + 10


def test_quality_gate_rows_do_not_reset_no_progress(hermes_home):
    from hermes_cli import goals

    first = goals._goal_progress_fingerprint([
        {"tool": "quality gate", "call": "$ true", "timestamp": 1.0, "output": "exit 0 (passed)"},
    ])
    second = goals._goal_progress_fingerprint([
        {"tool": "quality gate", "call": "$ true", "timestamp": 9999.0, "output": "exit 0 (passed)"},
    ])
    assert first is None and second is None


def test_read_only_status_regex_rejects_destructive_variants(hermes_home):
    from hermes_cli import goals

    assert goals._READ_ONLY_STATUS_CALL_RE.match("git branch")
    assert goals._READ_ONLY_STATUS_CALL_RE.match("find . -name '*.tmp'")
    assert not goals._READ_ONLY_STATUS_CALL_RE.match("git branch -D old")
    assert not goals._READ_ONLY_STATUS_CALL_RE.match("find . -delete")


def test_legacy_goal_row_defaults_new_backoff_fields():
    from hermes_cli.goals import GoalState

    state = GoalState.from_json(json.dumps({"goal": "old", "status": "active", "turns_used": 1}))
    assert state.consecutive_no_progress == 0
    assert state.last_progress_fingerprint is None
    assert state.last_wait_notice_key is None
    assert state.barrier_rearms == 0
    assert state.barrier_recheck_at == 0.0
