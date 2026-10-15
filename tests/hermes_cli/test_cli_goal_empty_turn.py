"""CLI evaluates successful empty turns but never a failed-turn notice."""

import pytest

from hermes_cli import goals
from tests.hermes_cli.test_cli_goal_interrupt import _make_cli_with_goal

pytest_plugins = ["tests.hermes_cli.test_cli_goal_interrupt"]


@pytest.mark.parametrize("text", ["", "   ", "[SILENT]", "failure notice"])
@pytest.mark.parametrize("outcome", ["success", "failed", "interrupted", "incomplete"])
def test_goal_turn_outcome_controls_cli_judging(hermes_home, monkeypatch, text, outcome):
    cli, manager = _make_cli_with_goal("cli-empty-turn")
    cli._last_turn_result = {"final_response": text}
    cli._last_turn_result[{"success": "completed", "failed": "failed",
                           "interrupted": "interrupted", "incomplete": "completed"}[outcome]] = outcome != "incomplete"
    cli.conversation_history = [{"role": "assistant", "content": "an earlier response"}]
    calls = []
    def judge(*args, **kwargs):
        calls.append(args[1])
        return "done", "tool results prove completion", False, None, False
    monkeypatch.setattr(goals, "judge_goal", judge)
    cli._maybe_continue_goal_after_turn()
    assert calls == ([text] if outcome == "success" else [])
    assert manager.state.turns_used == int(outcome == "success")
    assert manager.state.status == ("done" if outcome == "success" else "active")


@pytest.mark.parametrize("outcome", ["empty", "whitespace", "tool_only", "failed", "interrupted", "incomplete"])
def test_cli_settlement_uses_raw_result_not_rendered_failure_notice(hermes_home, monkeypatch, outcome):
    from cli import _ChatTurn
    from hermes_cli import loops
    from tests.hermes_cli.test_goal_judge_evidence import _record_tool_call

    cli, manager = _make_cli_with_goal("cli-settle-empty")
    db = goals._get_session_db()
    db.ensure_session(cli.session_id, source="cli")
    loop = loops.LoopManager(cli.session_id)
    loop.set("Check the deployment", interval_seconds=300, until="deployment healthy")
    assert loop.fire_tick()
    cli._loop_manager = loop
    cli._prompt_start_time = None
    cli._flush_stream = lambda: None
    cli._quiet_wakeup_turn = False
    cli._voice_continuous = cli._voice_tts = False
    cli._chat_resolve_interrupt = lambda *args: (None, False)
    cli._chat_print_reasoning_box = lambda *args: None
    rendered = []
    cli._chat_print_response_panel = lambda turn, response: rendered.append(response)
    cli._emit_focus_recovery_line = lambda: None
    cli._ring_bell = lambda **kwargs: None
    cli.conversation_history = [{"role": "assistant", "content": "earlier response [LOOP_COMPLETE]"}]
    cli.agent.session_id = cli.session_id
    cli.agent.max_iterations = 100
    turn = _ChatTurn()
    turn.result = {"final_response": "   " if outcome == "whitespace" else "",
                   "completed": outcome in {"empty", "whitespace", "tool_only"}}
    if outcome == "tool_only":
        _record_tool_call(db, cli.session_id, "terminal", {"command": "./bin/check"},
                          "Artifact verified", call_id="empty-tool")
    turn.result.update({
        "failed": {"failed": True, "error": "fixture provider failure"},
        "interrupted": {"interrupted": True, "final_response": "Interrupted partial answer"},
        "incomplete": {"final_response": "Incomplete response [LOOP_COMPLETE]"},
    }.get(outcome, {}))
    judged = []
    def judge(goal, response, **kwargs):
        judged.append((response, kwargs.get("evidence")))
        return "done", "runtime evidence verified", False, None, False
    monkeypatch.setattr(goals, "judge_goal", judge)
    cli._chat_settle_turn(turn)
    cli._chat_render_turn(turn, None, None)
    cli._maybe_continue_goal_after_turn()
    cli._maybe_complete_loop_tick_after_turn()
    successful = outcome in {"empty", "whitespace", "tool_only"}
    assert manager.state.turns_used == int(successful)
    assert manager.state.status == ("done" if successful else "active")
    assert len(judged) == int(successful)
    if outcome == "tool_only":
        assert any("Artifact verified" in item["output"] for item in judged[0][1])
    if outcome == "failed":
        assert rendered and rendered[0].strip()  # display copy is not goal progress
        assert cli._last_turn_result["final_response"] == ""
    after_loop = loops.load_loop(cli.session_id)
    assert after_loop.awaiting_response is False
    assert after_loop.status == "active"


def test_cli_started_turn_without_result_cannot_reuse_stale_success(hermes_home, monkeypatch):
    cli, manager = _make_cli_with_goal("cli-started-empty")
    cli._last_turn_started = True
    cli._last_turn_result = None
    cli.conversation_history = [{"role": "assistant", "content": "prior work [LOOP_COMPLETE]"}]
    judged = []

    def judge(*args, **kwargs):
        judged.append(args)
        return "done", "stale prior work", False, None, False

    monkeypatch.setattr(goals, "judge_goal", judge)
    cli._maybe_continue_goal_after_turn()
    assert judged == []
    assert manager.state.turns_used == 0
    assert manager.state.status == "active"


def test_cli_started_turn_without_result_cannot_reuse_stale_loop_marker(hermes_home):
    from hermes_cli import loops

    cli, _ = _make_cli_with_goal("cli-started-loop-empty")
    loop = loops.LoopManager(cli.session_id)
    loop.set("watch the deploy", interval_seconds=300)
    assert loop.fire_tick()
    cli._loop_manager = loop
    cli._last_turn_started = True
    cli._last_turn_result = None
    cli.conversation_history = [{"role": "assistant", "content": "prior work [LOOP_COMPLETE]"}]
    cli._maybe_complete_loop_tick_after_turn()
    after = loops.load_loop(cli.session_id)
    assert after.status == "active"
    assert after.awaiting_response is False


def test_cli_early_failure_cannot_reuse_previous_success(hermes_home, monkeypatch):
    cli, manager = _make_cli_with_goal("cli-setup-failure")
    cli._last_turn_result = {"completed": True, "final_response": "earlier successful response"}
    cli.conversation_history = [{"role": "assistant", "content": "earlier successful response"}]
    cli._secret_capture_callback = lambda *args: None
    cli._ensure_runtime_credentials = lambda: False
    judged = []
    def judge(*args, **kwargs):
        judged.append(args)
        return "done", "earlier work", False, None, False
    monkeypatch.setattr(goals, "judge_goal", judge)
    assert cli.chat("try again") is None
    cli._maybe_continue_goal_after_turn()
    assert judged == []
    assert manager.state.turns_used == 0
    assert manager.state.status == "active"
