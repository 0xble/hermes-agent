"""Real-path S2 matrix for external waits and no-progress backoff.

These tests deliberately enter through the CLI post-turn hook (or the gateway
classifier) instead of replacing ``collect_goal_evidence`` or ``judge_goal``.
Only the auxiliary judge response and process liveness are stubbed.
"""

from __future__ import annotations

import json
import queue
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

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
