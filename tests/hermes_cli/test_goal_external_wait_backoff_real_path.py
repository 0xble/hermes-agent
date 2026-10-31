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
    cli._last_turn_started = True
    cli._goal_turn_user_initiated = user_initiated
    return cli


def _record_terminal_turn(
    db, sid: str, command: str, output: str, index: int, *, arguments: str | None = None,
) -> None:
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
                "function": {"name": "terminal", "arguments": arguments if arguments is not None
                              else json.dumps({"command": command})},
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


def _record_tool_result(
    db, sid: str, tool_name: str, arguments: dict, output: str, index: int,
) -> None:
    """Persist one real assistant tool-call/result pair for a non-terminal tool."""
    call_id = f"real-{tool_name}-{index}-{uuid.uuid4().hex}"
    timestamp = time.time()
    db.append_message(
        sid,
        "assistant",
        "",
        timestamp=timestamp,
        tool_calls=[{
            "id": call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(arguments)},
        }],
    )
    db.append_message(
        sid,
        "tool",
        output,
        tool_name=tool_name,
        tool_call_id=call_id,
        timestamp=timestamp + 0.001,
    )


def _discard_continuation(cli) -> None:
    if not cli._pending_input.empty():
        cli._pending_input.get_nowait()


@pytest.mark.parametrize("surface", ["cli", "tui"])
def test_real_surface_preserves_wait_armed_during_turn(hermes_home, monkeypatch, surface):
    from hermes_cli import goals

    sid, _db, manager = _new_goal()
    turn_started_at = time.time() - 10
    manager.wait_for_seconds(86_400, reason="report is due tomorrow")
    assert manager.state is not None
    until = manager.state.waiting_until
    calls = []
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "more work"}, calls)
    result = {"completed": True, "final_response": "waiting for tomorrow"}
    if surface == "cli":
        cli = _cli(manager, user_initiated=True)
        cli._last_turn_started_at = turn_started_at
        cli._last_turn_result = result
        cli.conversation_history = [{"role": "assistant", "content": result["final_response"]}]
        cli._maybe_continue_goal_after_turn()
        assert cli._pending_input.empty()
    else:
        from tui_gateway import server

        monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(server, "_active_goal_manager", lambda _session: manager)
        monkeypatch.setattr(server, "_plan_goal_compression_recovery", lambda *_a, **_kw: (None, None))
        followup = server._goal_followup_after_turn(
            sid, {"session_key": sid, "agent": SimpleNamespace(session_id=sid)},
            result, "complete", result["final_response"], turn_started_at=turn_started_at,
        )
        assert followup is None
    assert calls == []
    state = goals.load_goal(sid)
    assert state is not None
    assert state.waiting_until == until
    assert state.turns_used == 0


def test_real_repeated_judge_waits_use_idle_lift_backoff_and_one_notice(
    hermes_home, monkeypatch,
):
    """P1 red path: repeated timed WAITs must back off instead of re-polling every minute."""
    from hermes_cli import goals

    _sid, _db, manager = _new_goal()
    cli = _cli(manager, user_initiated=False)
    _stub_judge(
        monkeypatch,
        {"verdict": "wait", "wait_for_seconds": 30, "reason": "external consolidation"},
    )
    clock = [10_000.0]
    monkeypatch.setattr(goals.time, "time", lambda: clock[0])

    waits = []
    notices = []
    with patch("cli._cprint"):
        for cycle in range(4):
            cli.conversation_history = [{"role": "assistant", "content": "No actionable work yet."}]
            decision = manager.evaluate_after_turn("No actionable work yet.", user_initiated=False)
            waits.append(manager.state.waiting_seconds)
            notices.append(decision["message"])

            # Exercise the real CLI idle lift between automatic judge turns.
            manager.state.waiting_until = clock[0] - 1
            manager._save()
            cli._last_goal_barrier_check = 0.0
            cli._maybe_resume_parked_goal()
            assert not cli._pending_input.empty(), cycle
            cli._pending_input.get_nowait()
            if cycle < 3:
                # The resumed continuation is now the automatic-chain anchor. Advance past the
                # 15-minute floor before exercising the next idle lift.
                clock[0] += 900

    # Automatic judge WAITs are floored at the current no-progress rung and advance it.
    assert waits == list(goals._NO_PROGRESS_BACKOFF_S)
    assert waits == sorted(waits)
    assert manager.state.status == "active"
    assert sum("Goal parked (judge)" in message for message in notices) == 1
    assert manager.state.last_wait_notice_key == "timed|reason:external consolidation"


def test_real_timed_wait_expiry_reparks_through_evaluator_with_one_notice(
    hermes_home, monkeypatch,
):
    """B1: evaluator expiry preserves the key while the automatic floor advances."""
    _sid, _db, manager = _new_goal()
    _stub_judge(
        monkeypatch,
        {"verdict": "wait", "wait_for_seconds": 30, "reason": "external consolidation"},
    )

    first = manager.evaluate_after_turn("No actionable work yet.", user_initiated=False)
    assert first["message"].count("Goal parked") == 1
    assert manager.state.waiting_seconds == 900
    manager.state.waiting_until = time.time() - 1
    manager._save()

    second = manager.evaluate_after_turn("Still waiting on the external result.", user_initiated=False)
    assert second["message"] == ""
    assert manager.state.waiting_seconds == 3600
    assert manager.state.last_wait_notice_key == "timed|reason:external consolidation"
    assert manager.state.backoff_level == 2


def test_real_judge_wait_mixed_continue_and_wait_uses_shared_backoff_formula(
    hermes_home, monkeypatch,
):
    """B3: CONTINUE, CONTINUE, WAIT, WAIT shares the 900/3600/14400/86400 ladder."""
    _sid, _db, manager = _new_goal()
    import agent.auxiliary_client as auxiliary

    responses = iter([
        {"verdict": "continue", "reason": "still checking"},
        {"verdict": "continue", "reason": "still checking"},
        {"verdict": "wait", "wait_for_seconds": 60, "reason": "still checking"},
        {"verdict": "wait", "wait_for_seconds": 60, "reason": "still checking"},
    ])
    monkeypatch.setattr(auxiliary, "call_llm", lambda **_kwargs: _judge_response(next(responses)))

    waits = []
    for _ in range(4):
        if manager.state.waiting_until:
            manager.state.waiting_until = time.time() - 1
            manager._save()
        decision = manager.evaluate_after_turn("No actionable work yet.", user_initiated=False)
        # Automatic no-progress CONTINUEs park at once, exactly like judge WAITs.
        assert decision["verdict"] == "wait"
        waits.append(manager.state.waiting_seconds)
    assert waits == [900, 3600, 14400, 86400]
    assert manager.state.last_wait_notice_key == "timed|reason:still checking"

    # A requested 1200-second wait outranks the first automatic floor (900).
    _sid2, _db2, manager2 = _new_goal()
    monkeypatch.setattr(
        auxiliary, "call_llm",
        lambda **_kwargs: _judge_response(
            {"verdict": "wait", "wait_for_seconds": 1200, "reason": "scheduled check"}
        ),
    )
    manager2.evaluate_after_turn("No actionable work yet.", user_initiated=False)
    assert manager2.state.waiting_seconds == 1200


def test_real_judge_wait_with_active_delegations_uses_delegation_floor_and_key(
    hermes_home, monkeypatch,
):
    """B4: an active delegation floors a timed WAIT at max(600, current rung)."""
    from hermes_cli import goals

    _sid, _db, manager = _new_goal()
    _stub_judge(
        monkeypatch,
        {"verdict": "wait", "wait_for_seconds": 60, "reason": "delegated review"},
    )
    decision = manager.evaluate_after_turn(
        "The delegated review is still running.", user_initiated=False, active_delegations=2,
    )
    assert decision["verdict"] == "wait"
    assert manager.state.waiting_seconds == max(600, goals._NO_PROGRESS_BACKOFF_S[0])
    assert manager.state.waiting_on_delegations == 2
    assert manager.state.last_wait_notice_key == "delegations|reason:delegated review"


def test_real_actionable_evidence_and_user_turn_reset_shared_wait_streak(hermes_home, monkeypatch):
    """P1: one actionable SessionDB result or a real user turn resets the shared streak."""
    from hermes_cli import goals

    sid, db, manager = _new_goal()
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "waiting for external work"})

    def expire():
        manager.state.waiting_until = time.time() - 1
        manager._save()

    manager.evaluate_after_turn("No action.", user_initiated=False)
    expire()
    manager.evaluate_after_turn("No action.", user_initiated=False)
    assert manager.state.consecutive_no_progress == 2
    assert manager.state.backoff_level == 2
    expire()

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
    assert manager.state.backoff_level == 0

    manager.evaluate_after_turn("No action.", user_initiated=False)
    expire()
    manager.evaluate_after_turn("No action.", user_initiated=False)
    assert manager.state.consecutive_no_progress == 2
    assert manager.is_waiting()
    # A real user turn pierces the live wait, re-judges, and resets the ladder.
    decision = manager.evaluate_after_turn("The user supplied a new instruction.", user_initiated=True)
    assert decision["verdict"] == "continue"
    assert manager.state.consecutive_no_progress == 0
    assert manager.state.backoff_level == 0


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
        cli._last_turn_result = {"completed": True, "final_response": "I checked CI status."}
        cli._maybe_continue_goal_after_turn()
        if index < 3:
            _discard_continuation(cli)

    state = goals.GoalManager(sid).state
    assert state is not None
    assert state.consecutive_no_progress == goals.DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS
    assert state.waiting_until > state.waiting_since
    assert state.last_reason == "CI is still running"


def test_real_write_survives_eight_read_only_results_per_automatic_turn(
    hermes_home, monkeypatch,
):
    """P1: per-turn classification sees a write even when the judge ledger is capped at eight."""
    from hermes_cli import goals

    sid, db, manager = _new_goal()
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "keep working"})
    clock = [time.time()]
    monkeypatch.setattr(goals.time, "time", lambda: clock[0])

    for turn in range(4):
        if turn:
            clock[0] += goals.DEFAULT_MIN_CONTINUATION_GAP_SECONDS + 1
        _record_tool_result(db, sid, "write_file", {"path": f"turn-{turn}.txt", "content": "changed"},
                            "wrote file", turn)
        for read in range(9):
            _record_tool_result(db, sid, "read_file", {"path": f"turn-{turn}-read-{read}.txt"},
                                "read file", turn * 10 + read)
        decision = manager.evaluate_after_turn("I made progress.", user_initiated=False)
        assert decision["should_continue"] is True
        assert manager.state is not None
        assert manager.state.consecutive_no_progress == 0
        assert manager.state.waiting_until == 0.0


def test_real_pause_resume_resets_no_progress_backoff_before_status_turn(
    hermes_home, monkeypatch,
):
    """P2: resuming a parked goal starts a fresh automatic no-progress streak."""
    from hermes_cli import goals

    sid, db, manager = _new_goal()
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "still waiting"})

    for _ in range(goals.DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS + 1):
        manager.evaluate_after_turn("No local action.", user_initiated=False)
        state = manager.state
        assert state is not None
        if state.waiting_until:
            state.waiting_until = time.time() - 1
            manager._save()
    state = manager.state
    assert state is not None
    assert state.backoff_level == 2
    assert state.waiting_seconds == 3600

    manager.pause()
    state = manager.state
    assert state is not None
    assert state.consecutive_no_progress == 0
    assert state.backoff_level == 0
    manager.resume(reset_budget=False)
    state = manager.state
    assert state is not None
    assert state.consecutive_no_progress == 0
    assert state.backoff_level == 0

    _record_tool_result(db, sid, "read_file", {"path": "status.txt"}, "status", 100)
    decision = manager.evaluate_after_turn("I checked the status.", user_initiated=False)
    state = manager.state
    assert state is not None
    # The fresh ladder starts again at its first rung.
    assert decision["should_continue"] is False
    assert state.consecutive_no_progress == 1
    assert state.waiting_seconds == goals._NO_PROGRESS_BACKOFF_S[0]
    assert state.backoff_level == 1


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
        "git status\nrm -r build",
        "git status & rm -r build",
        "git diff <(cat x)",
        "git diff --output=x",
        "git diff --ext-diff",
        "git diff --textconv",
        "rg --pre cat x",
        "find . -fprint x",
        "find . -fprint0 x",
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
    cli._last_turn_result = {"completed": True, "final_response": "I ran the command."}
    cli._maybe_continue_goal_after_turn()

    state = goals.GoalManager(sid).state
    assert state is not None
    assert state.consecutive_no_progress == 0
    assert state.waiting_until == 0.0


def test_real_truncated_terminal_arguments_fail_closed_as_actionable(hermes_home, monkeypatch):
    """B2: malformed collector JSON never grants a read-only prefix exemption."""
    from hermes_cli import goals

    sid, db, manager = _new_goal()
    cli = _cli(manager, user_initiated=False)
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "keep working"})
    _record_terminal_turn(
        db, sid, "git status", "command output", 1,
        arguments='{"command": "git status"',
    )
    cli.conversation_history = [{"role": "assistant", "content": "I ran the command."}]
    cli._last_turn_result = {"completed": True, "final_response": "I ran the command."}
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
    cli._last_turn_result = {"completed": True, "final_response": "No new action."}
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
        # Built exactly as _post_turn_goal_continuation enqueues it (provenance-marked).
        event = GatewayRunner._synthetic_prompt_event(
            source, prompt, reply_expected=False, goal_continuation=True,
        )
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
    def chat(*_args, **_kwargs):
        tui.conversation_history = [{"role": "assistant", "content": "automatic result"}]
        tui._last_turn_started = True
        tui._last_turn_result = {"completed": True, "final_response": "automatic result"}

    tui.chat = chat
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


def test_real_delegation_no_progress_uses_sessiondb_evidence_and_lifts_early(
    hermes_home, monkeypatch,
):
    """P2: real status evidence plus a live delegation parks for max(600s, rung), then lifts on return."""
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
        cli._last_turn_result = {"completed": True, "final_response": "I checked the pending review."}
        _stub_judge(monkeypatch, {"verdict": "continue", "reason": "waiting on delegated review"})
        cli._maybe_continue_goal_after_turn()
        state = goals.GoalManager(sid).state
        assert state is not None
        assert state.waiting_on_delegations == 1
        assert state.waiting_seconds == max(10 * 60, goals._NO_PROGRESS_BACKOFF_S[0])
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


def test_real_exited_target_lifts_barrier_for_a_continuation(
    hermes_home, monkeypatch,
):
    """An exited target clears the live barrier and returns to normal judging."""
    from hermes_cli import goals

    sid, db, manager = _new_goal()
    calls: list[dict] = []
    _stub_judge(monkeypatch, {"verdict": "wait", "wait_on_session": "watcher-exited", "reason": "watcher"}, calls)
    manager.evaluate_after_turn("Waiting on the watcher.", user_initiated=True)
    monkeypatch.setattr(goals, "_session_waiting", lambda _session_id: False)
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "inspect the completed watcher result"}, calls)
    # The continuation acts on the watcher's result, so it is progress rather than a ladder park.
    _record_tool_result(db, sid, "write_file", {"path": "watcher-result.md", "content": "ok"}, "wrote file", 1)

    decision = goals.GoalManager(sid).evaluate_after_turn(
        "The watcher exited; inspect its result.", user_initiated=False,
    )
    state = goals.GoalManager(sid).state
    assert decision["verdict"] == "continue"
    assert state is not None and state.waiting_on_session is None
    assert len(calls) == 2


def test_classic_cli_settle_keeps_turn_start_for_goal_hook(hermes_home, monkeypatch):
    """Settle clears the status-bar timer before the goal hook runs; the turn start must survive it."""
    from cli import HermesCLI, _ChatTurn
    from hermes_cli import goals

    sid, _db, manager = _new_goal()
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = sid
    cli.agent = SimpleNamespace(session_id=sid, max_iterations=100)
    cli._goal_manager = manager
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = False
    cli._goal_turn_user_initiated = True
    cli.conversation_history = []
    cli._flush_stream = lambda: None
    started = time.time() - 10
    cli._prompt_start_time = started
    cli._last_turn_started_at = started
    manager.wait_for_seconds(86_400, reason="report is due tomorrow")
    until = manager.state.waiting_until
    calls = []
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "more work"}, calls)
    turn = _ChatTurn()
    turn.result = {"completed": True, "final_response": "waiting for tomorrow",
                   "messages": [{"role": "assistant", "content": "waiting for tomorrow"}]}
    cli._chat_settle_turn(turn)
    assert cli._prompt_start_time is None
    cli._maybe_continue_goal_after_turn()
    assert calls == []
    assert cli._pending_input.empty()
    state = goals.load_goal(sid)
    assert state is not None and state.waiting_until == until
