"""GoalManager -> gateway -> adapter notice contract, backed by isolated SQLite.

Only the auxiliary verdict and external liveness are controlled. Continuation
admission, automatic-turn provenance, CAS lift, evaluator commit, post-delivery
notice creation and durable reload use production code.
"""
from __future__ import annotations

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
        _restart_db()
        judge.reset_mock()
        mgr = await _turn(harness, marker)
        judge.assert_called_once()
        assert judge.call_args.args[1] == marker
    assert adapter.sent == []
    assert mgr.state.last_continuation_notice_key == previous
    assert mgr.state.status == "active"
    assert mgr.state.consecutive_no_progress == 1
    assert key in adapter._pending_messages  # silence must not disable goal execution


@pytest.mark.asyncio
@pytest.mark.parametrize("disputed", [False, True])
@pytest.mark.parametrize("read_only_tool", [None, "read_file", "terminal"])
async def test_silent_no_evidence_stall_stops_with_one_notice_after_reload(hermes_home, disputed, read_only_tool):
    harness = _harness(hermes_home)
    _, adapter, entry, _, key = harness
    limit = goals.DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS
    for i in range(limit):
        adapter._pending_messages.clear()  # consume the previously admitted turn
        _restart_db()
        if read_only_tool:
            db = goals._get_session_db()
            assert db is not None
            db.ensure_session(entry.session_id, source="telegram")
            arguments = {"command": "git status --short"} if read_only_tool == "terminal" else {"path": "release.md"}
            db.append_message(entry.session_id, "assistant", "", tool_calls=[{
                "id": f"read-only-{i}", "type": "function",
                "function": {"name": read_only_tool, "arguments": json.dumps(arguments)},
            }])
            db.append_message(entry.session_id, "tool", f"Unchanged release status {i}",
                              tool_name=read_only_tool, tool_call_id=f"read-only-{i}")
        with patch.object(goals, "judge_goal", return_value=(
            "continue", f"Criterion still lacks proof ({i})", False,
            {"disputed": True} if disputed else None, False,
        )):
            mgr = await _turn(harness, "NO_REPLY" if i % 2 else "[SILENT]")
        if i < limit - 1:
            assert mgr.is_active() and key in adapter._pending_messages
            assert adapter.sent == []
    assert not mgr.is_active() and not mgr.has_goal()
    assert mgr.state.status == "cleared", "a silent stall must not pause waiting for the user"
    assert not mgr.is_parked() and mgr.state.waiting_until == 0
    assert mgr.state.backoff_level == 0
    assert mgr.next_continuation_prompt() is None
    assert key not in adapter._pending_messages
    assert len(adapter.sent) == 1
    assert "Goal stopped" in adapter.sent[0] and "no new evidence" in adapter.sent[0]
    assert "/goal resume" not in adapter.sent[0]
    _restart_db()
    with patch.object(goals, "judge_goal") as judge:
        # Already-admitted stale turns and a later user message must not replay the notice.
        await _turn(harness, "NO_REPLY", user=True)
        await _turn(harness, "status", user=True)
        judge.assert_not_called()
    assert len(adapter.sent) == 1
    assert GoalManager(entry.session_id).state.status == "cleared"


@pytest.mark.asyncio
@pytest.mark.parametrize("disputed", [False, True])
@pytest.mark.parametrize("interleave", [None, "backoff", "timed-wait", "delegation-wait"])
async def test_default_gap_silent_judgments_converge_across_nonjudging_waits(hermes_home, monkeypatch, disputed, interleave):
    harness = _harness(hermes_home)
    runner, adapter, entry, source, key = harness
    # Omit the gap override: exercise the actual gateway default/config reader.
    runner.config.goals = {"max_turns": 0}
    gap = runner._goal_min_continuation_gap_from_config()
    assert gap == goals.DEFAULT_MIN_CONTINUATION_GAP_SECONDS
    mgr = GoalManager(entry.session_id)
    mgr.set("Publish the wiki", max_turns=0)
    clock = [time.time() + 1]
    monkeypatch.setattr(goals.time, "time", lambda: clock[0])
    verdict = ("continue", REASON, False, {"disputed": True} if disputed else None, False)
    with patch.object(goals, "judge_goal", return_value=verdict) as judge:
        mgr = await _turn(harness, "[SILENT]")
        assert judge.call_count == 1 and mgr.state.consecutive_silent_no_progress == 1
        adapter._pending_messages.clear()
        if interleave:
            # These are already-established waits. Their held turns do not invoke the judge and
            # must neither invent a fresh notice nor erase the earlier silent judgment.
            if interleave == "backoff":
                mgr._no_progress_wait(mgr.state, "no progress yet")
            elif interleave == "timed-wait":
                mgr.wait_for_seconds(60, reason="external check pending")
            else:
                mgr.wait_for_seconds(60, reason="delegation pending", on_delegations=1)
            mgr.state.last_wait_notice_key = mgr._wait_notice_key(mgr.state)
            mgr._save()
            _restart_db()
            with patch.object(goals, "count_active_delegations", return_value=1 if interleave == "delegation-wait" else 0):
                mgr = await _turn(harness, "NO_REPLY")
            assert judge.call_count == 1
            assert mgr.state.consecutive_silent_no_progress == 1
            assert adapter.sent == []
            clock[0] = mgr.state.waiting_until + 1

        # The second actual judgment is followed by a pacing early return, not dispatch. The
        # increment must already be durable there, and the held turn must leave it unchanged.
        clock[0] += 1
        _restart_db()
        mgr = await _turn(harness, "NO_REPLY")
        assert judge.call_count == 2 and mgr.state.consecutive_silent_no_progress == 2
        assert goals.is_continuation_gap_wait(mgr.state)
        assert adapter.sent == [] and key not in adapter._pending_messages
        _restart_db()
        mgr = await _turn(harness, "[SILENT]")
        assert judge.call_count == 2 and mgr.state.consecutive_silent_no_progress == 2
        assert adapter.sent == []
        clock[0] = mgr.state.waiting_until + 1
        _restart_db()
        mgr = await _turn(harness, "[SILENT]")
        assert judge.call_count == 3
        assert mgr.state.consecutive_silent_no_progress == 3
        assert mgr.state.status == "cleared" and not mgr.has_goal()
        assert mgr.state.waiting_until == 0 and not mgr.is_parked()
        assert mgr.state.backoff_level == 0 and mgr.state.consecutive_no_progress == 0
        assert key not in adapter._pending_messages and mgr.next_continuation_prompt() is None
        assert len(adapter.sent) == 1 and adapter.sent[0].startswith("Goal stopped:")
        notice = list(adapter.sent)
        for _ in range(2):
            clock[0] += gap + 1
            _restart_db()
            await runner._post_turn_goal_continuation(
                session_entry=entry, source=source, final_response="NO_REPLY",
            )
        assert judge.call_count == 3 and adapter.sent == notice
        assert GoalManager(entry.session_id).state.status == "cleared"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("active_delegations", [0, 1])
async def test_silent_continue_repark_keeps_notice_identity_after_db_reload(hermes_home, active_delegations):
    """Synthetic NO_REPLY preserves the existing parked condition through backoff."""
    harness = _harness(hermes_home)
    _, adapter, entry, _, _ = harness
    with patch.object(goals, "count_active_delegations", return_value=active_delegations), patch.object(
        goals, "collect_goal_evidence", return_value=[]
    ), patch.object(goals, "judge_goal", return_value=("continue", REASON, False, None, False)):
        for _ in range(1 if active_delegations else goals.DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS):
            mgr = await _turn(harness, "Waiting for the external review.")
        first = [m for m in adapter.sent if m.startswith("⏳")]
        assert len(first) == 1
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
        assert [m for m in adapter.sent if m.startswith("⏳")] == first
        assert mgr.state.turns_used == original_turns + 2
        assert mgr.state.backoff_level >= original_level
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
            await _turn(harness, "The review returned a new blocker.")
        parked = [m for m in adapter.sent if m.startswith("⏳")]
        assert len(parked) == 2 and "new blocker" in parked[-1]
