"""GoalManager -> gateway -> adapter notice contract, backed by isolated SQLite.

Only the auxiliary verdict and external liveness are controlled. Continuation
admission, automatic-turn provenance, CAS lift, evaluator commit, post-delivery
notice creation and durable reload use production code.
"""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from hermes_cli import goals
from hermes_cli.goals import GoalManager
from tests.gateway.test_goal_continuation_silence import _runner_with_goal
from tests.gateway.test_goal_continuation_drain import hermes_home  # noqa: F401

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


def _restart_db():
    # A fresh handle must read from disk, not the previous manager or cache.
    for db in goals._DB_CACHE.values():
        db.close()
    goals._DB_CACHE.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["NO_REPLY", "[SILENT]"])
@pytest.mark.parametrize("prior_notice", [False, True])
async def test_silent_automatic_turn_never_manufactures_continuation_notice(hermes_home, marker, prior_notice):
    harness = _harness(hermes_home)
    _, adapter, entry, _, key = harness
    with patch.object(goals, "judge_goal", return_value=("continue", REASON, False, None, False)) as judge:
        if prior_notice:
            await _turn(harness, "A partial result is ready.", user=True)
        previous = GoalManager(entry.session_id).state.last_continuation_notice_key
        adapter.sent.clear()
        _restart_db()
        judge.reset_mock()
        mgr = await _turn(harness, marker)
        judge.assert_not_called()
    assert adapter.sent == []
    assert mgr.state.last_continuation_notice_key == previous
    assert mgr.state.status == "active"
    assert mgr.state.consecutive_no_progress == 1
    assert key in adapter._pending_messages  # silence must not disable goal execution


@pytest.mark.asyncio
@pytest.mark.parametrize("lift", ["timer-evaluator", "session-evaluator", "pid-evaluator", "idle-cas"])
async def test_same_reason_after_wait_lift_and_reload_is_not_reposted(hermes_home, lift):
    harness = _harness(hermes_home)
    _, adapter, entry, _, key = harness
    with patch.object(goals, "judge_goal", return_value=("continue", REASON, False, None, False)):
        mgr = await _turn(harness, "A partial result is ready.", user=True)
        assert len(adapter.sent) == 1 and REASON in adapter.sent[0]
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
        with patch.object(goals, "_session_waiting", return_value=False), patch.object(goals, "_pid_alive", return_value=False):
            mgr = await _turn(harness, "The release is still awaiting live validation.")
    assert len(adapter.sent) == 1, "unchanged reason was sent again after the barrier lift"
    assert mgr.state.last_continuation_notice_key == original_key
    assert not mgr.is_parked()
    assert key in adapter._pending_messages


@pytest.mark.asyncio
async def test_new_reason_after_silent_turn_is_deliverable(hermes_home):
    harness = _harness(hermes_home)
    _, adapter, entry, _, _ = harness
    with patch.object(goals, "judge_goal", return_value=("continue", REASON, False, None, False)):
        await _turn(harness, "Initial result.", user=True)
    await _turn(harness, "NO_REPLY")
    _restart_db()
    new_reason = "The canary found a new delivery failure requiring correction"
    with patch.object(goals, "judge_goal", return_value=("continue", new_reason, False, None, False)):
        # A material automatic report must remain visible after silence.
        await _turn(harness, "The canary found a delivery failure.")
        await _turn(harness, "The correction is in progress.", user=True)
    assert len(adapter.sent) == 2
    assert REASON in adapter.sent[0] and new_reason in adapter.sent[1]
    assert GoalManager(entry.session_id).state.last_continuation_notice_key.endswith(new_reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,prefix,status", [("done", "✓ Goal achieved", "done"), ("blocked", "⏸ Goal blocked", "paused")])
async def test_outcome_and_input_blocker_survive_silent_turn(hermes_home, verdict, prefix, status):
    harness = _harness(hermes_home)
    _, adapter, _, _, _ = harness
    await _turn(harness, "NO_REPLY")
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
    assert len(adapter.sent) == 2 and all(REASON in notice for notice in adapter.sent)
