"""Real-path S2 matrix for external waits and no-progress backoff.

These tests deliberately enter through the CLI post-turn hook (or the gateway
classifier) instead of replacing ``collect_goal_evidence`` or ``judge_goal``.
Only the auxiliary judge response and process liveness are stubbed.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hermes_cli.cli_loops_mixin import CLILoopsMixin, _is_self_injected_turn


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli import goals

    token = set_hermes_home_override(str(home))
    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield home
    reset_hermes_home_override(token)
    goals._DB_CACHE.clear()


def _judge_response(payload: dict):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
    )


def _stub_judge(monkeypatch, payload: dict, calls: list[dict] | None = None):
    import agent.auxiliary_client as auxiliary

    def call_llm(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return _judge_response(payload)

    monkeypatch.setattr(auxiliary, "call_llm", call_llm)


def _new_goal():
    from hermes_cli import goals

    sid = f"real-s2-{uuid.uuid4().hex}"
    db = goals._get_session_db()
    db.ensure_session(sid, source="test")
    manager = goals.GoalManager(sid, default_max_turns=20)
    manager.set("wait for external consolidation", max_turns=20)
    return sid, db, manager


class _RealCLI(CLILoopsMixin):
    """The actual CLI post-turn hook with only its normal runtime state supplied."""


def _cli(manager, *, user_initiated: bool):
    cli = _RealCLI()
    cli.session_id = manager.session_id
    cli.agent = SimpleNamespace(session_id=manager.session_id)
    cli._goal_manager = manager
    cli._pending_input = queue.Queue()
    cli.conversation_history = []
    cli._last_turn_interrupted = False
    cli._goal_turn_user_initiated = user_initiated
    return cli


def _record_terminal_turn(db, sid: str, command: str, output: str, index: int) -> None:
    """Persist the same assistant tool-call + tool-result rows the runtime writes."""
    call_id = f"real-call-{index}"
    timestamp = time.time()
    db.append_message(
        sid,
        "assistant",
        "",
        timestamp=timestamp,
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "terminal", "arguments": json.dumps({"command": command})},
            }
        ],
    )
    db.append_message(
        sid,
        "tool",
        json.dumps({"output": output, "exit_code": 0}),
        tool_name="terminal",
        tool_call_id=call_id,
        timestamp=timestamp + 0.001,
    )


def _discard_continuation(cli) -> None:
    if not cli._pending_input.empty():
        cli._pending_input.get_nowait()


def test_real_repeated_judge_waits_use_idle_lift_backoff_and_one_notice(
    hermes_home, monkeypatch,
):
    """P1 red path: repeated timed WAITs must back off instead of re-polling every minute."""
    from hermes_cli import goals

    sid, _db, manager = _new_goal()
    cli = _cli(manager, user_initiated=False)
    _stub_judge(
        monkeypatch,
        {"verdict": "wait", "wait_for_seconds": 30, "reason": "external consolidation"},
    )

    waits = []
    notices = []
    with patch("cli._cprint"):
        for cycle in range(4):
            cli.conversation_history = [{"role": "assistant", "content": "No actionable work yet."}]
            decision = manager.evaluate_after_turn("No actionable work yet.", user_initiated=False)
            waits.append(manager.state.waiting_seconds)
            notices.append(decision["message"])

            # Exercise the real CLI idle lift between automatic judge turns.
            manager.state.waiting_until = time.time() - 1
            manager._save()
            cli._last_goal_barrier_check = 0.0
            cli._maybe_resume_parked_goal()
            assert not cli._pending_input.empty(), cycle
            cli._pending_input.get_nowait()

    assert waits == [300, 900, 1800, 1800]
    assert all(300 <= seconds <= 1800 for seconds in waits)
    assert waits == sorted(waits)
    assert sum("Goal parked (judge)" in message for message in notices) == 1
    assert manager.state.last_wait_notice_key == "timed|reason:external consolidation"


def test_real_actionable_evidence_and_user_turn_reset_shared_wait_streak(hermes_home, monkeypatch):
    """P1: one actionable SessionDB result or a real user turn resets the shared streak."""
    from hermes_cli import goals

    sid, db, manager = _new_goal()
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "waiting for external work"})
    manager.evaluate_after_turn("No action.", user_initiated=False)
    manager.evaluate_after_turn("No action.", user_initiated=False)
    assert manager.state.consecutive_no_progress == 2

    call_id = "real-actionable-call"
    stamp = time.time()
    db.append_message(
        sid, "assistant", "", timestamp=stamp,
        tool_calls=[{"id": call_id, "type": "function", "function": {
            "name": "apply_patch", "arguments": json.dumps({"patch": "*** Begin patch"})}}],
    )
    db.append_message(
        sid, "tool", "patch applied", tool_name="apply_patch", tool_call_id=call_id,
        timestamp=stamp + 0.001,
    )
    manager.evaluate_after_turn("I applied the requested change.", user_initiated=False)
    assert manager.state.consecutive_no_progress == 0

    manager.evaluate_after_turn("No action.", user_initiated=False)
    manager.evaluate_after_turn("No action.", user_initiated=False)
    assert manager.state.consecutive_no_progress == 2
    manager.evaluate_after_turn("The user supplied a new instruction.", user_initiated=True)
    assert manager.state.consecutive_no_progress == 0


def test_real_session_evidence_drives_three_status_continuations_to_backoff(
    hermes_home, monkeypatch,
):
    """P1: JSON tool arguments from SessionDB must classify real status polls as read-only."""
    from hermes_cli import goals

    sid, db, manager = _new_goal()
    cli = _cli(manager, user_initiated=False)
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "CI is still running"})

    for index, output in enumerate(("queued", "in_progress", "completed"), start=1):
        time.sleep(0.01)
        _record_terminal_turn(db, sid, "gh run view 1", output, index)
        cli.conversation_history = [{"role": "assistant", "content": "I checked CI status."}]
        cli._maybe_continue_goal_after_turn()
        if index < 3:
            _discard_continuation(cli)

    state = goals.GoalManager(sid).state
    assert state is not None
    assert state.consecutive_no_progress == goals.DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS
    assert state.waiting_until > state.waiting_since
    assert state.last_reason == "CI is still running"


def test_real_collector_preserves_json_argument_representation(hermes_home):
    """The persisted evidence shape is JSON text; classification must parse it before matching."""
    from hermes_cli import goals

    sid, db, _manager = _new_goal()
    _record_terminal_turn(db, sid, "gh run view 1", "queued", 1)

    evidence = goals.collect_goal_evidence(sid)
    assert evidence[-1]["call"] == '{"command": "gh run view 1"}'
    assert json.loads(evidence[-1]["call"])["command"] == "gh run view 1"


@pytest.mark.parametrize(
    "command",
    [
        "git status && rm -r build",
        "git status || rm -r build",
        "git status; rm -r build",
        "git status `rm -r build`",
        "git status $(rm -r build)",
        "find . -exec rm {} ;",
        "find . -execdir rm {} ;",
        "find . -ok rm {} ;",
    ],
)
def test_real_evidence_classifier_rejects_shell_operators(hermes_home, monkeypatch, command):
    """P2: chained/exec shell syntax is actionable, never a read-only status poll."""
    from hermes_cli import goals

    sid, db, manager = _new_goal()
    cli = _cli(manager, user_initiated=False)
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "keep working"})
    _record_terminal_turn(db, sid, command, "command output", 1)
    cli.conversation_history = [{"role": "assistant", "content": "I ran the command."}]
    cli._maybe_continue_goal_after_turn()

    state = goals.GoalManager(sid).state
    assert state is not None
    assert state.consecutive_no_progress == 0
    assert state.waiting_until == 0.0


def test_real_passing_quality_gate_rows_do_not_reset_no_progress(hermes_home, monkeypatch):
    """A passing gate is judge context, not progress evidence for the current turn."""
    from hermes_cli import goals

    sid, _db, manager = _new_goal()
    manager.add_gate("true")
    cli = _cli(manager, user_initiated=False)
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "still working"})
    cli.conversation_history = [{"role": "assistant", "content": "No new action."}]
    cli._maybe_continue_goal_after_turn()

    state = goals.GoalManager(sid).state
    assert state is not None
    assert state.consecutive_no_progress == 1


def test_real_continuation_builders_are_synthetic_to_cli_and_gateway(hermes_home):
    """P2: every standing-goal continuation builder carries automatic-turn provenance."""
    from gateway.run import GatewayRunner
    from hermes_cli import goals

    prompts = []
    _, _, plain = _new_goal()
    prompts.append(plain.next_continuation_prompt())

    _, _, contracted = _new_goal()
    contracted.set("contracted goal", contract=goals.GoalContract(outcome="a finished artifact"))
    prompts.append(contracted.next_continuation_prompt())

    _, _, subgoaled = _new_goal()
    subgoaled.set("goal with criteria")
    subgoaled.add_subgoal("also preserve the migration note")
    prompts.append(subgoaled.next_continuation_prompt())

    _, _, gated = _new_goal()
    gated.add_gate("python -c 'import sys; sys.exit(1)'", max_retries=1)
    gate_decision = gated._check_gates()
    prompts.append(gate_decision["continuation_prompt"])

    assert all(prompts)
    for prompt in prompts:
        assert _is_self_injected_turn(prompt)
        assert GatewayRunner._is_goal_continuation_event(prompt)


async def _gateway_runner_for_goal(monkeypatch, sid: str):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner

    class _AsyncStore:
        def __init__(self, store):
            self._store = store

        async def get_or_create_session(self, _source, *, touch_activity=True):
            return SimpleNamespace(session_id=sid)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = SimpleNamespace()
    runner._async_session_store = _AsyncStore(runner.session_store)
    runner._goal_max_turns_from_config = lambda: 20
    runner._warm_goals_session_db = AsyncMock()
    runner._run_in_executor_with_context = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    runner._post_turn_loop_completion = AsyncMock()
    runner._delivery_adapter_for = lambda _source: None
    return runner


@pytest.mark.asyncio
async def test_real_gateway_post_turn_hooks_mark_continuation_gate_failure_and_idle_wake_automatic(
    hermes_home, monkeypatch,
):
    """P2: real gateway events stay automatic all the way into the evaluator."""
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from hermes_cli import goals

    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "still working"})
    seen: list[bool] = []
    real_evaluate = goals.GoalManager.evaluate_after_turn

    def observe_evaluate(self, *args, **kwargs):
        seen.append(kwargs.get("user_initiated"))
        return real_evaluate(self, *args, **kwargs)

    monkeypatch.setattr(goals.GoalManager, "evaluate_after_turn", observe_evaluate)
    source = SessionSource(platform=Platform.DISCORD, chat_id="chat-s2", chat_type="channel")
    prompts = []
    for index in range(3):
        sid, _db, manager = _new_goal()
        if index == 0:
            prompt = manager.next_continuation_prompt()
        elif index == 1:
            manager.add_gate("python -c 'import sys; sys.exit(1)'", max_retries=1)
            prompt = manager._check_gates()["continuation_prompt"]
        else:
            prompt = manager.next_continuation_prompt()
        runner = await _gateway_runner_for_goal(monkeypatch, sid)
        event = GatewayRunner._synthetic_prompt_event(source, prompt)
        await GatewayRunner._run_post_turn_hooks(
            runner, agent_result={"final_response": "still working"}, source=source,
            is_internal=False, event=event,
        )
        prompts.append((event, prompt))

    assert [event.text.startswith("[Continuing toward your standing goal]") for event, _ in prompts] == [True, False, True]
    assert seen == [False, False, False]


def test_real_cli_tui_input_provenance_reaches_goal_evaluator_as_automatic(hermes_home, monkeypatch):
    """P2: the CLI/TUI input entry point, not a pre-set flag, classifies injected text."""
    from hermes_cli import goals
    from hermes_cli.cli_loops_mixin import CLILoopsMixin
    from hermes_cli.cli_tui_runtime_mixin import CLITuiRuntimeMixin

    class _RealTUI(CLITuiRuntimeMixin, CLILoopsMixin):
        pass

    sid, _db, manager = _new_goal()
    tui = _RealTUI()
    tui.session_id = sid
    tui.agent = SimpleNamespace(session_id=sid)
    tui._goal_manager = manager
    tui._pending_input = queue.Queue()
    tui._pending_resume_sessions = []
    tui._app = SimpleNamespace(invalidate=lambda: None, is_running=False)
    tui._tui_unwrap_input = lambda value: (value, False, False)
    tui._typed_voice_stop = lambda _value: False
    tui.handle_bang_shell = lambda _value: False
    tui._print_user_message_preview = lambda _value: None
    tui._turn_summary_begin = lambda: None
    tui._tui_after_turn = lambda: tui._maybe_continue_goal_after_turn()
    tui.chat = lambda *_args, **_kwargs: setattr(
        tui, "conversation_history", [{"role": "assistant", "content": "automatic result"}])
    tui.conversation_history = []
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "still working"})
    seen: list[bool] = []
    real_evaluate = goals.GoalManager.evaluate_after_turn

    def observe_evaluate(self, *args, **kwargs):
        seen.append(kwargs.get("user_initiated"))
        return real_evaluate(self, *args, **kwargs)

    monkeypatch.setattr(goals.GoalManager, "evaluate_after_turn", observe_evaluate)
    tui._tui_process_one_input(manager.next_continuation_prompt())
    assert seen == [False]


def test_real_tui_followup_dispatch_reaches_goal_followup_as_automatic(hermes_home, monkeypatch):
    """P2: follow-up dispatch passes user_turn=False into the prompt-turn evaluator."""
    from hermes_cli import goals
    from tui_gateway import prompt_turn, server

    sid, _db, manager = _new_goal()
    session = {"session_key": sid, "history_lock": threading.Lock(), "running": True}
    seen = {}
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "still working"})
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_active_goal_manager", lambda _session: manager)
    monkeypatch.setattr(server, "_plan_goal_compression_recovery", lambda *_args, **_kwargs: (None, None))

    def fake_submit(_rid, _sid, _session, _text, **kwargs):
        seen["user_turn"] = kwargs.get("user_turn", False)
        server._goal_followup_after_turn(
            _sid, _session, {"final_response": "automatic result"}, "complete", "automatic result",
            user_initiated=kwargs.get("user_turn", False),
        )
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", fake_submit)
    real_evaluate = goals.GoalManager.evaluate_after_turn
    monkeypatch.setattr(
        goals.GoalManager, "evaluate_after_turn",
        lambda self, *args, **kwargs: (seen.__setitem__("evaluator_user_turn", kwargs.get("user_initiated"))
                                        or real_evaluate(self, *args, **kwargs)),
    )
    server._dispatch_followup_turn(
        "rid", sid, session, manager.next_continuation_prompt(), "goal continuation")
    assert seen == {"user_turn": False, "evaluator_user_turn": False}


@pytest.mark.asyncio
async def test_real_gateway_idle_age_notice_delivery_dedupes_on_second_due_scan(hermes_home, monkeypatch):
    """P2: gateway idle delivery sends one wait-age notice and cross-path dedupe holds."""
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from hermes_cli import goals

    class _Adapter:
        _message_handler = object()
        _active_sessions = {}
        _pending_messages = {}

    sid, _db, manager = _new_goal()
    manager.wait_on_session("watcher-gateway-age", reason="external watcher")
    state = manager.state
    state.waiting_since = time.time() - goals._MAX_BARRIER_WAIT_S - 1
    state.barrier_recheck_at = 0.0
    manager._save()
    monkeypatch.setattr(goals, "_session_waiting", lambda _sid: True)

    source = SessionSource(platform=Platform.DISCORD, chat_id="chat-age", chat_type="channel")
    entry = SimpleNamespace(session_id=sid, session_key="gateway-age-key", origin=source, suspended=False,
                            resume_pending=False)
    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(lookup_by_session_id=lambda value: entry if value == sid else None)
    runner._restored_source = lambda value: value.origin
    runner._delivery_adapter_for = lambda _source: _Adapter()
    runner._is_session_running = lambda _key: False
    runner._queue_depth = lambda _key, adapter=None: 0
    runner._session_key_for_source = lambda _source: entry.session_key
    runner._goal_max_turns_from_config = lambda: 20
    runner._run_in_executor_with_context = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    runner._send_goal_status_notice = AsyncMock()

    await GatewayRunner._goal_wakeup_fire_one(runner, sid)
    assert runner._send_goal_status_notice.await_count == 1
    assert runner._send_goal_status_notice.await_args.kwargs["notice_kind"] == "wait-age"

    st = goals.GoalManager(sid).state
    st.barrier_recheck_at = 0.0
    goals.save_goal(sid, st)
    await GatewayRunner._goal_wakeup_fire_one(runner, sid)
    assert runner._send_goal_status_notice.await_count == 1

    # The evaluator may commit the age notice before the idle scanner gets the row.
    sid2, _db2, manager2 = _new_goal()
    manager2.wait_on_session("watcher-evaluator-age", reason="external watcher")
    manager2.state.waiting_since = time.time() - goals._MAX_BARRIER_WAIT_S - 1
    manager2.state.barrier_recheck_at = 0.0
    manager2._save()
    decision = manager2.evaluate_after_turn("The user asked for a status.", user_initiated=True)
    assert "30 minutes" in decision["message"]
    entry.session_id = sid2
    manager2.state.barrier_recheck_at = 0.0
    manager2._save()
    await GatewayRunner._goal_wakeup_fire_one(runner, sid2)
    assert runner._send_goal_status_notice.await_count == 1


def test_real_tui_idle_age_notice_reaches_status_and_dedupes(hermes_home, monkeypatch):
    """P2: the TUI idle owner emits wait-age through its status surface once."""
    from hermes_cli import goals
    from tui_gateway import session_notifications

    sid, _db, manager = _new_goal()
    manager.wait_on_session("watcher-tui-age", reason="external watcher")
    manager.state.waiting_since = time.time() - goals._MAX_BARRIER_WAIT_S - 1
    manager.state.barrier_recheck_at = 0.0
    manager._save()
    monkeypatch.setattr(goals, "_session_waiting", lambda _sid: True)
    monkeypatch.setattr(session_notifications, "_notif_gateway_owns_heartbeat", lambda *_args: False)
    notices = []
    monkeypatch.setattr(session_notifications, "_notif_loop_status", lambda _sid, text: notices.append(text))
    session = {"session_key": sid, "running": False}

    session_notifications._maybe_resume_tui_parked_goal("tui-age", session)
    st = goals.GoalManager(sid).state
    st.barrier_recheck_at = 0.0
    goals.save_goal(sid, st)
    session_notifications._maybe_resume_tui_parked_goal("tui-age", session)
    assert len(notices) == 1 and "30 minutes" in notices[0]


def test_real_cli_idle_age_notice_reaches_user_and_dedupes(hermes_home, monkeypatch):
    """P2: the CLI idle owner prints wait-age once, even when a second scan is due."""
    from hermes_cli import goals

    sid, _db, manager = _new_goal()
    manager.wait_on_session("watcher-cli-age", reason="external watcher")
    manager.state.waiting_since = time.time() - goals._MAX_BARRIER_WAIT_S - 1
    manager.state.barrier_recheck_at = 0.0
    manager._save()
    monkeypatch.setattr(goals, "_session_waiting", lambda _sid: True)
    cli = _cli(manager, user_initiated=False)
    cli._last_goal_barrier_check = 0.0
    with patch("cli._cprint") as cprint:
        cli._maybe_resume_parked_goal()
        manager.state.barrier_recheck_at = 0.0
        manager._save()
        cli._last_goal_barrier_check = 0.0
        cli._maybe_resume_parked_goal()
    age_lines = [call.args[0] for call in cprint.call_args_list if "30 minutes" in call.args[0]]
    assert len(age_lines) == 1


def test_real_exited_after_rearm_lifts_only_after_live_barrier_rechecks(hermes_home, monkeypatch):
    """P2: a re-armed live target never lifts at 5/15/30 minutes, then exits once."""
    from hermes_cli import goals

    sid, _db, manager = _new_goal()
    manager.wait_on_session("watcher-rearm-exit", reason="watcher")
    manager.state.waiting_since = 1000.0
    manager.state.barrier_recheck_at = 0.0
    manager._save()
    monkeypatch.setattr(goals, "_session_waiting", lambda _sid: True)

    now = 1000.0 + goals._MAX_BARRIER_WAIT_S + 1
    with patch.object(goals.time, "time", return_value=now):
        assert manager.rearm_live_barrier() is not None
    assert manager.state.barrier_rearms > 0
    assert manager.state.barrier_recheck_at > now
    assert manager.lifted_barrier_prompt() is None

    for offset in (5 * 60, 15 * 60, 30 * 60):
        manager.state.barrier_recheck_at = now + offset - 1
        manager._save()
        with patch.object(goals.time, "time", return_value=now + offset):
            notice = manager.rearm_live_barrier()
            assert notice is None or isinstance(notice, str)
            assert manager.lifted_barrier_prompt() is None

    monkeypatch.setattr(goals, "_session_waiting", lambda _sid: False)
    cli = _cli(manager, user_initiated=False)
    cli._last_goal_barrier_check = 0.0
    with patch("cli._cprint"):
        cli._maybe_resume_parked_goal()
    assert cli._pending_input.qsize() == 1
    assert "wait for external consolidation" in cli._pending_input.get_nowait()
    assert manager.state.waiting_on_session is None
    cli._last_goal_barrier_check = 0.0
    with patch("cli._cprint"):
        cli._maybe_resume_parked_goal()
    assert cli._pending_input.empty()


def test_real_delegation_no_progress_uses_sessiondb_evidence_and_lifts_early(
    hermes_home, monkeypatch,
):
    """P2: real status evidence plus a live delegation parks for ten minutes, then lifts on return."""
    from hermes_cli import goals
    from tools import async_delegation as delegation

    sid, db, manager = _new_goal()
    _record_terminal_turn(db, sid, "gh pr checks 302", "pending", 1)
    record_id = f"real-delegation-{uuid.uuid4().hex}"
    with delegation._records_lock:
        delegation._records[record_id] = {
            "status": "running", "parent_session_id": sid, "session_key": "",
            "origin_ui_session_id": "",
        }
    try:
        assert goals.count_active_delegations(sid) == 1
        cli = _cli(manager, user_initiated=False)
        cli.conversation_history = [{"role": "assistant", "content": "I checked the pending review."}]
        _stub_judge(monkeypatch, {"verdict": "continue", "reason": "waiting on delegated review"})
        cli._maybe_continue_goal_after_turn()
        state = goals.GoalManager(sid).state
        assert state is not None
        assert state.waiting_on_delegations == 1
        assert state.waiting_seconds == 10 * 60
        assert state.waiting_until > state.waiting_since

        with delegation._records_lock:
            delegation._records[record_id]["status"] = "completed"
        assert goals.GoalManager(sid).is_waiting() is False
        cli._last_goal_barrier_check = 0.0
        with patch("cli._cprint"):
            cli._maybe_resume_parked_goal()
        assert cli._pending_input.qsize() == 1
        assert "wait for external consolidation" in cli._pending_input.get_nowait()
    finally:
        with delegation._records_lock:
            delegation._records.pop(record_id, None)


def test_real_judge_wait_then_evaluator_age_notice_reaches_cli_user(
    hermes_home, monkeypatch, capsys,
):
    """P2: a judge WAIT followed by an aged user turn must emit the 30-minute notice."""
    from hermes_cli import goals

    sid, _db, manager = _new_goal()
    cli = _cli(manager, user_initiated=True)
    calls: list[dict] = []
    _stub_judge(
        monkeypatch,
        {"verdict": "wait", "wait_on_session": "watcher-age", "reason": "watcher still running"},
        calls,
    )
    monkeypatch.setattr(goals, "_session_waiting", lambda _session_id: True)

    cli.conversation_history = [{"role": "assistant", "content": "Waiting on the watcher."}]
    cli._maybe_continue_goal_after_turn()
    parked = manager.state
    assert parked is not None and parked.waiting_on_session == "watcher-age"

    parked.waiting_since = time.time() - goals._MAX_BARRIER_WAIT_S - 1
    parked.barrier_recheck_at = 0.0
    manager._save()
    cli.conversation_history = [{"role": "assistant", "content": "The user asked for a status update."}]
    cli._maybe_continue_goal_after_turn()

    output = capsys.readouterr().out
    assert "30 minutes" in output
    assert len(calls) == 1
    aged = goals.GoalManager(sid).state
    assert aged is not None and aged.last_age_notice_key


def test_real_judge_wait_then_idle_rearm_delivers_age_notice(
    hermes_home, monkeypatch,
):
    """The idle barrier entry point emits the same notice when no user turn arrives."""
    from hermes_cli import goals

    sid, _db, manager = _new_goal()
    _stub_judge(monkeypatch, {"verdict": "wait", "wait_on_session": "watcher-idle", "reason": "external watcher"})
    manager.evaluate_after_turn("Waiting on the watcher.", user_initiated=True)
    state = goals.GoalManager(sid).state
    assert state is not None
    state.waiting_since = time.time() - goals._MAX_BARRIER_WAIT_S - 1
    state.barrier_recheck_at = 0.0
    goals.save_goal(sid, state)
    monkeypatch.setattr(goals, "_session_waiting", lambda _session_id: True)

    notice = goals.GoalManager(sid).rearm_live_barrier()
    assert notice is not None
    assert "30 minutes" in notice
    assert goals.GoalManager(sid).state.last_age_notice_key


def test_real_judge_wait_rearms_live_barrier_at_five_fifteen_and_thirty_minutes(
    hermes_home, monkeypatch,
):
    """Live barriers remain armed through the escalating 5/15/30-minute probes."""
    from unittest.mock import patch

    from hermes_cli import goals

    sid, _db, manager = _new_goal()
    _stub_judge(monkeypatch, {"verdict": "wait", "wait_on_session": "watcher-rechecks", "reason": "watcher"})
    manager.evaluate_after_turn("Waiting on the watcher.", user_initiated=True)
    state = goals.GoalManager(sid).state
    assert state is not None
    state.waiting_since = 1000.0
    state.barrier_recheck_at = 0.0
    goals.save_goal(sid, state)
    monkeypatch.setattr(goals, "_session_waiting", lambda _session_id: True)

    with patch.object(goals.time, "time", return_value=1000.0 + goals._MAX_BARRIER_WAIT_S + 1):
        assert goals.GoalManager(sid).rearm_live_barrier()
    state = goals.GoalManager(sid).state
    assert state.barrier_recheck_at == pytest.approx(1000.0 + goals._MAX_BARRIER_WAIT_S + 1 + 5 * 60)

    with patch.object(goals.time, "time", return_value=1000.0 + goals._MAX_BARRIER_WAIT_S + 1 + 5 * 60 + 1):
        goals.GoalManager(sid).rearm_live_barrier()
    state = goals.GoalManager(sid).state
    assert state.barrier_recheck_at == pytest.approx(1000.0 + goals._MAX_BARRIER_WAIT_S + 1 + 5 * 60 + 15 * 60 + 1)

    with patch.object(goals.time, "time", return_value=state.barrier_recheck_at + 1):
        goals.GoalManager(sid).rearm_live_barrier()
    state = goals.GoalManager(sid).state
    assert state.barrier_recheck_at > 0.0
    assert state.waiting_on_session == "watcher-rechecks"


def test_real_judge_wait_at_six_hours_pauses_without_a_second_judge(
    hermes_home, monkeypatch,
):
    """A still-live target reaches the six-hour pause through the evaluator path."""
    from unittest.mock import patch

    from hermes_cli import goals

    sid, _db, manager = _new_goal()
    calls: list[dict] = []
    _stub_judge(monkeypatch, {"verdict": "wait", "wait_on_session": "watcher-hard-cap", "reason": "watcher"}, calls)
    manager.evaluate_after_turn("Waiting on the watcher.", user_initiated=True)
    state = goals.GoalManager(sid).state
    assert state is not None
    state.waiting_since = 1000.0
    state.barrier_recheck_at = 0.0
    goals.save_goal(sid, state)
    monkeypatch.setattr(goals, "_session_waiting", lambda _session_id: True)

    with patch.object(goals.time, "time", return_value=1000.0 + goals._MAX_LIVE_BARRIER_S + 1):
        decision = goals.GoalManager(sid).evaluate_after_turn("The user checked the watcher.", user_initiated=True)

    assert decision["status"] == "paused"
    assert "watcher-hard-cap" in decision["message"]
    assert len(calls) == 1


def test_real_exited_target_lifts_barrier_for_a_continuation(
    hermes_home, monkeypatch,
):
    """An exited target clears the live barrier and returns to normal judging."""
    from hermes_cli import goals

    sid, _db, manager = _new_goal()
    calls: list[dict] = []
    _stub_judge(monkeypatch, {"verdict": "wait", "wait_on_session": "watcher-exited", "reason": "watcher"}, calls)
    manager.evaluate_after_turn("Waiting on the watcher.", user_initiated=True)
    monkeypatch.setattr(goals, "_session_waiting", lambda _session_id: False)
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "inspect the completed watcher result"}, calls)

    decision = goals.GoalManager(sid).evaluate_after_turn(
        "The watcher exited; inspect its result.", user_initiated=False,
    )
    state = goals.GoalManager(sid).state
    assert decision["verdict"] == "continue"
    assert state is not None and state.waiting_on_session is None
    assert len(calls) == 2
