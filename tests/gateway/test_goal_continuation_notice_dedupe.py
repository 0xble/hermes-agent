"""GoalManager -> gateway -> adapter notice contract, backed by isolated SQLite.

Only the auxiliary verdict and external liveness are controlled. Continuation
admission, automatic-turn provenance, CAS lift, evaluator commit, post-delivery
notice creation and durable reload use production code.
"""
from __future__ import annotations

import uuid
import json
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from hermes_cli import goals
from hermes_cli.goals import GoalManager
from tests.gateway.test_goal_continuation_silence import _runner_with_goal
from tests.gateway.test_goal_continuation_drain import hermes_home as hermes_home

REASON = "The release still needs live validation"


def _harness(home):
    runner, adapter, entry, source, key = _runner_with_goal(home)
    runner.config.goals = {"min_continuation_gap_seconds": 0, "max_turns": 0}
    return runner, adapter, entry, source, key


async def _turn(harness, response, *, user=False):
    runner, adapter, entry, source, key = harness
    if user:
        event = MessageEvent(text="status?", message_type=MessageType.TEXT, source=source)
    else:
        event = runner._synthetic_prompt_event(
            source, GoalManager(entry.session_id).next_continuation_prompt(),
            reply_expected=False, goal_continuation=True,
        )
    before = len(adapter.sent)
    await runner._run_post_turn_hooks(agent_result=response, source=source, is_internal=False, event=event)
    # No notice is sent before the final response's delivery boundary.
    assert len(adapter.sent) == before
    callback = adapter.pop_post_delivery_callback(key)
    if callback:
        await callback()
    return GoalManager(entry.session_id)


def _lift_wait(entry):
    """Let the current quiet ladder rung elapse, as the idle wakeup ticker would observe it."""
    mgr = GoalManager(entry.session_id)
    if mgr.state.waiting_until:
        mgr.state.waiting_until = 1.0
        mgr._save()


def _restart_db():
    # A fresh handle must read from disk, not the previous manager or cache.
    for db in goals._DB_CACHE.values():
        db.close()
    goals._DB_CACHE.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["NO_REPLY", "[SILENT]"])
@pytest.mark.parametrize("prior_notice", [False, True])
async def test_silent_automatic_turn_rejudges_without_manufacturing_continuation_notice(hermes_home, marker, prior_notice):
    harness = _harness(hermes_home)
    _, adapter, entry, _, key = harness
    with patch.object(goals, "judge_goal", return_value=("continue", REASON, False, None, False)) as judge:
        if prior_notice:
            await _turn(harness, "A partial result is ready.", user=True)
        previous = GoalManager(entry.session_id).state.last_continuation_notice_key
        adapter.sent.clear()
        adapter._pending_messages.clear()  # drop the user turn's own continuation
        _restart_db()
        judge.reset_mock()
        mgr = await _turn(harness, marker)
        judge.assert_called_once()
        assert judge.call_args.args[1] == marker
    assert adapter.sent == []
    assert mgr.state.last_continuation_notice_key == previous
    assert mgr.state.status == "active"
    assert mgr.state.consecutive_no_progress == 1
    assert key not in adapter._pending_messages  # quiet no-progress turns park silently
    assert mgr.state.waiting_until > mgr.state.waiting_since


@pytest.mark.asyncio
@pytest.mark.parametrize("lift", ["timer-evaluator", "session-evaluator", "pid-evaluator", "idle-cas"])
async def test_same_reason_after_wait_lift_and_reload_is_not_reposted(hermes_home, lift):
    harness = _harness(hermes_home)
    _, adapter, entry, _, key = harness
    with patch.object(goals, "judge_goal", return_value=("continue", REASON, False, None, False)):
        mgr = await _turn(harness, "A partial result is ready.", user=True)
        assert adapter.sent == []  # continuing notices are now log-only
        original_key = mgr.state.last_continuation_notice_key
        if lift == "session-evaluator":
            mgr.wait_on_session("completed-watcher", reason="wait for validation")
        elif lift == "pid-evaluator":
            with patch.object(goals, "_pid_alive", return_value=True):
                mgr.wait_on(424242, reason="wait for validation")
        else:
            mgr.wait_for_seconds(60, reason="wait for validation")
        if lift in ("timer-evaluator", "idle-cas"):
            mgr.state.waiting_until = 1.0
            mgr._save()
        _restart_db()
        mgr = GoalManager(entry.session_id)
        if lift == "idle-cas":
            assert mgr.clear_lifted_wait(mgr.state.waiting_since)
            _restart_db()
            # A new interpreter must retain the same identity after restart.
            subprocess.run(
                [sys.executable, "-c",
                 "import sys; from hermes_cli.goals import GoalManager; "
                 "m=GoalManager(sys.argv[1]); assert not m.is_parked(); "
                 "assert m.state.last_continuation_notice_key == sys.argv[2]",
                 entry.session_id, original_key],
                check=True, capture_output=True, text=True,
            )
        # This test exercises notice identity, not pacing; place the synthetic wake beyond the
        # hard automatic-continuation floor.
        mgr.state.last_automatic_continuation_at = time.time() - goals.DEFAULT_MIN_CONTINUATION_GAP_SECONDS - 1
        mgr._save()
        with patch.object(goals, "_session_waiting", return_value=False), patch.object(goals, "_pid_alive", return_value=False), \
                patch.object(goals, "_evidence_only_read_only_status", return_value=False), \
            patch.object(goals, "_goal_progress_fingerprint", side_effect=lambda _e: uuid.uuid4().hex):
            mgr = await _turn(harness, "The release is still awaiting live validation.")
    assert adapter.sent == []  # unchanged continuing notices remain log-only
    assert mgr.state.last_continuation_notice_key == original_key
    assert not mgr.is_parked()
    assert key in adapter._pending_messages


@pytest.mark.asyncio
async def test_new_reason_after_silent_turn_is_deliverable(hermes_home):
    harness = _harness(hermes_home)
    _, adapter, entry, _, _ = harness
    with patch.object(goals, "judge_goal", return_value=("continue", REASON, False, None, False)):
        await _turn(harness, "Initial result.", user=True)
        await _turn(harness, "NO_REPLY")  # quiet no-progress turn: parks silently on the ladder
    _lift_wait(entry)
    _restart_db()
    new_reason = "The canary found a new delivery failure requiring correction"
    with patch.object(goals, "judge_goal", return_value=("continue", new_reason, False, None, False)):
        # A material automatic report (real work this turn) must remain visible after silence.
        with patch.object(goals, "_evidence_only_read_only_status", return_value=False), \
            patch.object(goals, "_goal_progress_fingerprint", side_effect=lambda _e: uuid.uuid4().hex):
            await _turn(harness, "The canary found a delivery failure.")
        await _turn(harness, "The correction is in progress.", user=True)
    assert adapter.sent == []  # routine continuing notices are log-only
    assert GoalManager(entry.session_id).state.last_continuation_notice_key.endswith(new_reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,prefix,status", [("done", "✓ Goal achieved", "done"), ("blocked", "⏸ Goal blocked", "paused")])
async def test_outcome_and_input_blocker_survive_silent_turn(hermes_home, verdict, prefix, status):
    harness = _harness(hermes_home)
    _, adapter, entry, _, _ = harness
    with patch.object(goals, "judge_goal", return_value=("continue", REASON, False, None, False)):
        await _turn(harness, "NO_REPLY")  # quiet no-progress turn: parks silently on the ladder
    assert adapter.sent == []
    _lift_wait(entry)
    _restart_db()
    with patch.object(goals, "judge_goal", return_value=(verdict, "A material result needs attention", False, None, False)):
        mgr = await _turn(harness, "The canary result is available.")
    assert len(adapter.sent) == 1 and adapter.sent[0].startswith(prefix)
    assert mgr.state.status == status


@pytest.mark.asyncio
async def test_explicit_goal_controls_still_confirm_and_reset_notice_identity(hermes_home):
    harness = _harness(hermes_home)
    runner, adapter, entry, source, _ = harness
    with patch.object(goals, "judge_goal", return_value=("continue", REASON, False, None, False)):
        await _turn(harness, "Initial result.", user=True)
        for command, word in [("pause", "paused"), ("resume", "resumed")]:
            event = MessageEvent(text=f"/goal {command}", message_type=MessageType.TEXT, source=source)
            confirmation = await runner._handle_goal_command(event)
            assert word in confirmation.lower()
        assert GoalManager(entry.session_id).state.last_continuation_notice_key is None
        await _turn(harness, "A newly requested retry.", user=True)
    assert adapter.sent == []  # explicit controls still work, but continuing notices stay quiet


@pytest.mark.asyncio
@pytest.mark.parametrize("active_delegations", [0, 1])
async def test_silent_continue_repark_keeps_notice_identity_after_db_reload(hermes_home, active_delegations):
    """Quiet ladder parks stay silent and keep their notice identity through backoff and reload."""
    harness = _harness(hermes_home)
    _, adapter, entry, _, _ = harness
    with patch.object(goals, "count_active_delegations", return_value=active_delegations), patch.object(
        goals, "collect_goal_evidence", return_value=[]
    ), patch.object(goals, "judge_goal", return_value=("continue", REASON, False, None, False)):
        for _ in range(1 if active_delegations else goals.DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS):
            mgr = await _turn(harness, "Waiting for the external review.")
        assert mgr.is_parked()
        assert adapter.sent == []  # the no-progress ladder is quiet, with or without delegations
        original_key = mgr.state.last_wait_notice_key
        original_turns = mgr.state.turns_used
        original_level = mgr.state.backoff_level
        for _ in range(2):
            mgr.state.waiting_until = 1.0
            mgr._save()
            _restart_db()
            # Silent wakes are still judged (PR #531): recorded evidence, not prose, decides completion.
            # A judged CONTINUE on an intentional silent wake keeps the parked notice identity.
            with patch.object(goals, "judge_goal", return_value=("continue", REASON, False, None, False)) as judge:
                mgr = await _turn(harness, "NO_REPLY")
                judge.assert_called_once()
            assert mgr.state.last_wait_notice_key == original_key
            assert mgr.state.status == "active" and mgr.is_parked()
        assert adapter.sent == []
        assert mgr.state.turns_used == original_turns + 2
        assert mgr.state.backoff_level == original_level + 2  # each silent re-park climbs the ladder
        # Prove the durable identity in an actual fresh interpreter too.
        subprocess.run(
            [sys.executable, "-c",
             "import sys; from hermes_cli.goals import GoalManager; "
             "m=GoalManager(sys.argv[1]); assert m.is_parked(); "
             "assert m.state.last_wait_notice_key == sys.argv[2]",
             entry.session_id, original_key], check=True, capture_output=True, text=True,
        )
        mgr.state.waiting_until = 1.0
        mgr._save()
        new_reason = "review returned a new blocker"
        with patch.object(goals, "judge_goal", return_value=("continue", new_reason, False, None, False)):
            mgr = await _turn(harness, "The review returned a new blocker.")
        # A new judge reason alone is not progress: still a silent ladder park, never auto-cleared.
        assert adapter.sent == []
        assert mgr.state.status == "active" and mgr.is_parked()
        with patch.object(goals, "judge_goal", return_value=("continue", new_reason, False, None, False)):
            mgr = await _turn(harness, "What is the review blocker?", user=True)
        # A user turn pierces the park, resets the ladder, and reports the new reason.
        assert adapter.sent == []  # user-triggered continuation status is also log-only
        assert mgr.state.backoff_level == 0 and not mgr.is_parked()
