"""Acceptance tests for the goal redesign, slices 1-5 and 7.

Plan: maintenance/goal-system-incident-20261014/redesign.md (each test names its slice).
Only the auxiliary judge reply (or the judge function where noted) is stubbed; state goes
through the real SessionDB-backed GoalManager and, for slice 2, the real gateway post-turn path.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    # The outage notice is process-wide; start every test outside an outage.
    goals._judge_outage_notice(failed=False)
    yield home
    goals._judge_outage_notice(failed=False)
    goals._DB_CACHE.clear()


def _manager(sid="redesign"):
    from hermes_cli.goals import GoalManager
    mgr = GoalManager(sid, default_max_turns=0)
    mgr.set("finish the long-running verification")
    return mgr


def _judge_reply(payload):
    """A real auxiliary-client reply shape, parsed by the real judge_goal."""
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


# --- Slice 1: uncapped timed waits and wait_until -----------------------------------------


def test_slice_1_week_wait_persists_604800_seconds(hermes_home):
    from hermes_cli import goals

    mgr = _manager("slice-1-seconds")
    reply = _judge_reply({"verdict": "wait", "wait_for_seconds": 604800, "reason": "Sunday window"})
    before = time.time()
    with patch("agent.auxiliary_client.call_llm", return_value=reply):
        decision = mgr.evaluate_after_turn("Nothing to do until the Sunday window.")
    assert decision["verdict"] == "wait" and decision["status"] == "active"
    stored = goals.load_goal("slice-1-seconds")
    assert stored.waiting_seconds == 604800
    assert stored.waiting_until >= before + 604800
    assert goals.GoalManager("slice-1-seconds").is_waiting() is True


def test_slice_1_tax_goal_replay_parks_until_2026_10_20_0800_local(hermes_home, monkeypatch):
    """Replay of the tax goal: the judge names a local wall-clock time without an offset."""
    from hermes_cli import goals

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        # 2026-10-14 20:00 PDT, when the goal was last judged.
        now = datetime(2026, 10, 14, 20, 0).astimezone().timestamp()
        monkeypatch.setattr(goals.time, "time", lambda: now)
        mgr = _manager("slice-1-tax")
        reply = _judge_reply({
            "verdict": "wait", "wait_until": "2026-10-20T08:00:00",
            "reason": "the tax filing portal opens Tuesday 08:00",
        })
        with patch("agent.auxiliary_client.call_llm", return_value=reply):
            decision = mgr.evaluate_after_turn("Prepared everything; the portal is closed until Tuesday.")
        assert decision["verdict"] == "wait"
        stored = goals.load_goal("slice-1-tax")
        parked = datetime.fromtimestamp(stored.waiting_until).astimezone()
        assert parked.strftime("%Y-%m-%d %H:%M %Z") == "2026-10-20 08:00 PDT"
        assert stored.status == "active"
        # A synthetic wake before then holds without a judge call.
        judge = MagicMock()
        monkeypatch.setattr(goals.time, "time", lambda: now + 3 * 86400)
        with patch.object(goals, "judge_goal", judge):
            held = goals.GoalManager("slice-1-tax").evaluate_after_turn("", user_initiated=False)
        judge.assert_not_called()
        assert held["should_continue"] is False and held["status"] == "active"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


# --- Slice 2: delivered events re-judge a waiting goal -------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["cron", "process-notification"])
async def test_slice_2_delivered_event_mid_wait_makes_exactly_one_judge_call(hermes_home, delivery):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from gateway.session import SessionEntry, SessionSource, build_session_key
    from hermes_cli import goals

    src = SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="t", chat_type="dm")
    entry = SessionEntry(
        session_key=build_session_key(src), session_id="slice-2-gw", created_at=datetime.now(),
        updated_at=datetime.now(), platform=Platform.TELEGRAM, chat_type="dm", origin=src,
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")})
    runner._running, runner._running_agents, runner._running_agents_ts, runner._queued_events = True, {}, {}, {}
    adapter = SimpleNamespace(_pending_messages={}, _active_sessions={}, _message_handler=object(),
                              send=AsyncMock(return_value=SimpleNamespace(success=True, message_id="m")))
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store._generate_session_key.return_value = entry.session_key
    runner._is_session_running = lambda key: False
    runner._queue_depth = lambda key, adapter=None: 0

    async def _in_executor(fn, *args):
        return fn(*args)

    runner._run_in_executor_with_context = _in_executor

    mgr = goals.GoalManager(entry.session_id)
    mgr.set("file the quarterly report once the numbers land")
    until = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    with patch.object(goals, "judge_goal", return_value=("wait", "numbers land Monday", False, {"until": until}, False)):
        mgr.evaluate_after_turn("Waiting for the numbers.", user_initiated=True)
    assert goals.GoalManager(entry.session_id).is_waiting()

    # The goal's own pacing wake does not pierce the wait.
    continuation = runner._synthetic_prompt_event(
        src, goals.GoalManager(entry.session_id).next_continuation_prompt(), reply_expected=False,
        goal_continuation=True, goal_session_id=entry.session_id,
        goal_state=goals.GoalManager(entry.session_id).state,
    )
    with patch.object(goals, "judge_goal", return_value=("continue", "x", False, None, False)) as judge:
        await runner._run_post_turn_hooks(agent_result={"final_response": "nothing new"}, source=src,
                                          is_internal=True, event=continuation)
    assert judge.call_count == 0
    assert goals.GoalManager(entry.session_id).is_waiting()

    # Restart recovery re-runs an interrupted turn; it is not outside evidence either.
    for mark in ("restart-note", "resume-marker"):
        recovery = MessageEvent(text="", message_type=MessageType.TEXT, source=src, internal=True)
        if mark == "restart-note":
            recovery._restart_note_expected = (entry.session_id, "k", "m", True)
        else:
            recovery._outbox_admission_id = "resume:marker-1"
        with patch.object(goals, "judge_goal", return_value=("continue", "x", False, None, False)) as judge:
            await runner._run_post_turn_hooks(agent_result={"final_response": "resumed"}, source=src,
                                              is_internal=True, event=recovery)
        assert judge.call_count == 0, mark
        assert goals.GoalManager(entry.session_id).is_waiting(), mark

    # A delivered event is fresh evidence: exactly one judge call, wait lifted, continuation queued.
    if delivery == "cron":
        event = MessageEvent(text="Cron job 'numbers-watch' output: Q3 numbers posted.",
                             message_type=MessageType.TEXT, source=src, internal=True,
                             metadata={"notification_category": "result"})
    else:
        event = MessageEvent(text="[process finished]", message_type=MessageType.TEXT, source=src,
                             internal=True, metadata={"notification_origin": "process_registry_synthetic"})
    with patch.object(goals, "judge_goal", return_value=("continue", "numbers arrived", False, None, False)) as judge:
        await runner._run_post_turn_hooks(agent_result={"final_response": "The numbers are in."},
                                          source=src, is_internal=True, event=event)
    assert judge.call_count == 1
    state = goals.load_goal(entry.session_id)
    assert state.status == "active" and state.waiting_until == 0
    assert any(str(getattr(e, "text", "")).startswith("[Continuing toward your standing goal]")
               for e in adapter._pending_messages.values())


# --- Slice 3: one quiet no-progress ladder, nothing auto-clears ----------------------------


def test_slice_3_ten_silent_continues_stay_active_on_the_ladder(hermes_home, monkeypatch):
    from hermes_cli import goals

    clock = [time.time()]
    monkeypatch.setattr(goals.time, "time", lambda: clock[0])
    sid = "slice-3-ladder"
    _manager(sid)
    waits, decisions = [], []
    with patch("agent.auxiliary_client.call_llm",
               return_value=_judge_reply({"verdict": "continue", "reason": "awaiting Brian's window"})):
        for _ in range(10):
            mgr = goals.GoalManager(sid)  # fresh manager each wake, as after a restart
            decisions.append(mgr.evaluate_after_turn("[SILENT]", user_initiated=False))
            stored = goals.load_goal(sid)
            waits.append(stored.waiting_seconds)
            # The idle ticker wakes the goal once the rung elapses.
            clock[0] = stored.waiting_until + 1
    stored = goals.load_goal(sid)
    assert stored.status == "active"
    assert waits[:4] == [900, 3600, 14400, 86400]
    assert waits[4:] == [86400] * 6
    assert sum(bool(d.get("message")) for d in decisions) == 0
    assert all(d["status"] == "active" for d in decisions)
    assert not any(d["status"] == "cleared" for d in decisions)

    # A user message resets the ladder to the first rung.
    with patch("agent.auxiliary_client.call_llm",
               return_value=_judge_reply({"verdict": "continue", "reason": "keep going"})):
        goals.GoalManager(sid).evaluate_after_turn("Go ahead.", user_initiated=True)
        goals.GoalManager(sid).evaluate_after_turn("[SILENT]", user_initiated=False)
    assert goals.load_goal(sid).waiting_seconds == 900


# --- Slice 4: a judge outage never pauses ---------------------------------------------------


def test_slice_4_ten_goals_on_judge_http_500_end_unpaused_with_one_notice(hermes_home):
    from hermes_cli import goals

    class APIStatusError(Exception):
        status_code = 500

    managers = [_manager(f"slice-4-{i}") for i in range(10)]
    decisions = []
    with patch("agent.auxiliary_client.call_llm", side_effect=APIStatusError("HTTP 500 Internal Server Error")):
        for _round in range(3):
            for mgr in managers:
                decisions.append(mgr.evaluate_after_turn("still working", user_initiated=_round == 0))
    statuses = [goals.load_goal(f"slice-4-{i}").status for i in range(10)]
    assert statuses.count("paused") == 0 and set(statuses) == {"active"}
    notices = [d["message"] for d in decisions if d["message"]]
    assert notices == [goals.JUDGE_OUTAGE_NOTICE]

    # Recovery ends the outage; the next outage may announce once again.
    with patch("agent.auxiliary_client.call_llm",
               return_value=_judge_reply({"verdict": "continue", "reason": "back"})):
        managers[0].evaluate_after_turn("still working")
    with patch("agent.auxiliary_client.call_llm", side_effect=APIStatusError("HTTP 500")):
        again = [m.evaluate_after_turn("still working") for m in managers[:3]]
    assert [d["message"] for d in again if d["message"]] == [goals.JUDGE_OUTAGE_NOTICE]


# --- Slice 5: blocked on Brian is a user-message wait; a breach still pauses ---------------


def test_slice_5_blocked_on_brian_waits_for_his_message(hermes_home):
    from hermes_cli import goals

    sid = "slice-5-brian"
    mgr = _manager(sid)
    reply = _judge_reply({"verdict": "blocked", "reason": "Waiting for Brian to approve the Sunday cutover window"})
    with patch("agent.auxiliary_client.call_llm", return_value=reply):
        decision = mgr.evaluate_after_turn("Cutover plan ready; I need Brian's approval of the window.")
    assert decision["status"] == "active" and decision["should_continue"] is False
    stored = goals.load_goal(sid)
    assert stored.status == "active" and stored.waiting_on_user_message is True
    assert "approve the Sunday cutover window" in decision["message"]  # one line naming the decision

    # Automatic wakes hold without judging and without repeating the notice.
    judge = MagicMock()
    with patch.object(goals, "judge_goal", judge):
        held = goals.GoalManager(sid).evaluate_after_turn("", user_initiated=False)
    judge.assert_not_called()
    assert held["message"] == "" and held["status"] == "active"
    # No /goal resume chore: Brian's reply re-judges directly.
    with patch.object(goals, "judge_goal", return_value=("continue", "approved", False, None, False)) as judge:
        after = goals.GoalManager(sid).evaluate_after_turn("Approved, go Sunday.", user_initiated=True)
    assert judge.call_count == 1 and after["should_continue"] is True
    assert goals.load_goal(sid).waiting_on_user_message is False


def test_slice_5_irreversible_constraint_breach_still_pauses(hermes_home):
    from hermes_cli import goals

    sid = "slice-5-breach"
    mgr = _manager(sid)
    reply = _judge_reply({"verdict": "blocked",
                          "reason": "Continuing would breach the irreversible no-delete constraint"})
    with patch("agent.auxiliary_client.call_llm", return_value=reply):
        decision = mgr.evaluate_after_turn("The only path left deletes production data.")
    assert decision["status"] == "paused" and decision["message"]
    assert goals.load_goal(sid).status == "paused"


# --- Slice 7: removed breaker fields ------------------------------------------------------

_REMOVED_FIELDS = (
    "consecutive_silent_no_progress", "barrier_rearms", "barrier_recheck_at", "last_age_notice_key",
    "consecutive_disputes", "last_dispute_evidence", "last_continuation_at",
)


def test_slice_7_removed_breaker_fields_are_absent(hermes_home):
    from hermes_cli import goals
    from hermes_cli.goals import GoalState

    encoded = json.loads(GoalState(goal="x").to_json())
    for name in _REMOVED_FIELDS:
        assert name not in encoded
        assert not hasattr(GoalState(goal="x"), name)
    for name in ("_MAX_BARRIER_WAIT_S", "_MAX_LIVE_BARRIER_S", "_BARRIER_REARM_BACKOFF_S",
                 "DEFAULT_MAX_CONSECUTIVE_DISPUTES"):
        assert not hasattr(goals, name)
    assert not hasattr(goals.GoalManager, "rearm_live_barrier")
    assert "waiting_on_user_message" in encoded


def test_slice_7_old_rows_with_removed_fields_load_and_run(hermes_home):
    from hermes_cli import goals

    sid = "slice-7-legacy"
    _manager(sid)
    db = goals._get_session_db()
    row = json.loads(goals.load_goal(sid).to_json())
    row.update({
        "consecutive_silent_no_progress": 2, "barrier_rearms": 3, "barrier_recheck_at": 1.0,
        "last_age_notice_key": "live-barrier:pid 1|reason:x", "consecutive_disputes": 2,
        "last_dispute_evidence": "e1,e2", "last_continuation_at": 123.0,
    })
    db.set_meta(goals._meta_key(sid), json.dumps(row))
    goals._DB_CACHE.clear()
    loaded = goals.load_goal(sid)
    assert loaded is not None and loaded.status == "active"
    for name in _REMOVED_FIELDS:
        assert name not in json.loads(loaded.to_json())
    with patch.object(goals, "judge_goal", return_value=("continue", "more", False, None, False)):
        decision = goals.GoalManager(sid).evaluate_after_turn("did a step", user_initiated=True)
    assert decision["status"] == "active"
    for name in _REMOVED_FIELDS:
        assert name not in json.loads(goals.load_goal(sid).to_json())


# --- Review regressions: pacing never spins, outages back off, breaches naming Brian pause ---


def _repeat_evidence(*_a, **_k):
    # Non-read-only, but the same recorded result every turn: no new progress.
    return [{"tool": "terminal", "call": '{"command": "cd /repo && gh pr checks 611"}',
             "output": "pending", "timestamp": 1.0}]


def test_slice_3_repeated_non_read_only_evidence_backs_off(hermes_home, monkeypatch):
    from hermes_cli import goals

    clock = [time.time()]
    monkeypatch.setattr(goals.time, "time", lambda: clock[0])
    sid = "slice-3-repeat"
    _manager(sid)
    waits = []
    with patch.object(goals, "collect_goal_evidence", _repeat_evidence), \
            patch("agent.auxiliary_client.call_llm",
                  return_value=_judge_reply({"verdict": "continue", "reason": "CI still pending"})):
        for _ in range(5):
            d = goals.GoalManager(sid).evaluate_after_turn("checked CI, still pending", user_initiated=False)
            stored = goals.load_goal(sid)
            waits.append(stored.waiting_seconds)
            clock[0] = max(clock[0], stored.waiting_until) + 1
    # The first turn records the fingerprint as new progress; every repeat climbs the ladder.
    assert waits[1:] == [900, 3600, 14400, 86400]
    assert goals.load_goal(sid).status == "active"


def test_slice_4_persistent_outage_backs_off_on_the_ladder(hermes_home, monkeypatch):
    from hermes_cli import goals

    class APIStatusError(Exception):
        status_code = 500

    clock = [time.time()]
    monkeypatch.setattr(goals.time, "time", lambda: clock[0])
    sid = "slice-4-backoff"
    _manager(sid)
    waits, messages = [], []
    with patch.object(goals, "collect_goal_evidence", _repeat_evidence), \
            patch("agent.auxiliary_client.call_llm", side_effect=APIStatusError("HTTP 500")):
        for _ in range(5):
            d = goals.GoalManager(sid).evaluate_after_turn("working", user_initiated=False)
            messages.append(d["message"])
            stored = goals.load_goal(sid)
            waits.append(stored.waiting_seconds)
            clock[0] = max(clock[0], stored.waiting_until) + 1
    assert waits == [900, 3600, 14400, 86400, 86400]
    assert goals.load_goal(sid).status == "active"
    assert [m for m in messages if m] == [goals.JUDGE_OUTAGE_NOTICE]


@pytest.mark.parametrize("reason", [
    "The agent merged to main, which the goal's constraints forbid without Brian's sign-off.",
    "The agent already sent the external email the user said must not be sent; the user decides next steps.",
])
def test_slice_5_breach_naming_the_user_still_pauses(hermes_home, reason):
    from hermes_cli import goals

    sid = "slice-5-breach-user"
    mgr = _manager(sid)
    with patch("agent.auxiliary_client.call_llm",
               return_value=_judge_reply({"verdict": "blocked", "reason": reason})):
        decision = mgr.evaluate_after_turn("done", user_initiated=True)
    assert decision["status"] == "paused"
    assert goals.load_goal(sid).waiting_on_user_message is False


@pytest.mark.parametrize("reason", [
    "The goal's constraints require Brian's sign-off before merging; awaiting his approval.",
    "The agent must not send the DM until Brian approves the wording.",
    "The PR must not be merged until Brian approves it.",
])
def test_slice_5_constraint_requiring_approval_is_a_user_wait(hermes_home, reason):
    from hermes_cli import goals

    sid = "slice-5-approval"
    mgr = _manager(sid)
    with patch("agent.auxiliary_client.call_llm",
               return_value=_judge_reply({"verdict": "blocked", "reason": reason})):
        decision = mgr.evaluate_after_turn("ready", user_initiated=True)
    assert decision["status"] == "active"
    assert goals.load_goal(sid).waiting_on_user_message is True
