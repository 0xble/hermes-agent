"""TUI/Desktop successful empty turns remain goal evaluation boundaries."""

import pytest

from tui_gateway.prompt_turn import _is_successful_goal_turn


@pytest.mark.parametrize("text", ["", "   ", "[SILENT]", "failure notice"])
@pytest.mark.parametrize("outcome", ["success", "failed", "interrupted", "incomplete", "handoff"])
def test_goal_admission_uses_completion_not_reply_length(text, outcome):
    result = {"final_response": text}
    if outcome == "success":
        result["completed"] = True
    elif outcome == "failed":
        result["failed"] = True
    elif outcome == "interrupted":
        result["interrupted"] = True
    else:
        result["completed"] = False
        if outcome == "handoff":
            result["turn_exit_reason"] = "max_iterations_reached(3/3)"
    admitted = outcome == "success" or (outcome == "handoff" and bool(text.strip()))
    assert _is_successful_goal_turn(result, "complete", text) is admitted
    assert _is_successful_goal_turn(result, "error", text) is False
    assert _is_successful_goal_turn(result, "interrupted", text) is False


pytest_plugins = ["tests.tui_gateway.test_goal_command"]


@pytest.mark.parametrize("outcome", ["empty", "whitespace", "tool_only", "failed", "interrupted", "incomplete", "handoff"])
def test_tui_turn_evaluates_success_without_a_reply_and_releases_loop(server, turn_env, monkeypatch, outcome):
    from types import SimpleNamespace
    from hermes_cli import goals, loops
    from tests.hermes_cli.test_goal_judge_evidence import _record_tool_call
    from tests.tui_gateway.test_goal_command import _turn_session

    session_key = "tui-empty-integration"
    transcript_id = "tui-tool-transcript"
    goals.GoalManager(session_key).set("Verify the result")
    loop = loops.LoopManager(session_key)
    loop.set("Check the deployment", interval_seconds=300)
    assert loop.fire_tick()
    db = goals._get_session_db()
    db.ensure_session(transcript_id, source="cli")
    judged = []

    def run_conversation(*args, **kwargs):
        if outcome == "tool_only":
            _record_tool_call(db, transcript_id, "terminal", {"command": "./bin/check"},
                              "Artifact verified", call_id="empty-tool")
        result = {"final_response": "   " if outcome == "whitespace" else "",
                  "completed": outcome in {"empty", "whitespace", "tool_only"}}
        result.update({
            "failed": {"failed": True, "error": "fixture provider failure"},
            "interrupted": {"interrupted": True, "final_response": "Interrupted partial answer"},
            "incomplete": {"final_response": "Incomplete response [LOOP_COMPLETE]"},
            "handoff": {"final_response": "iteration summary", "turn_exit_reason": "max_iterations_reached(3/3)"},
        }.get(outcome, {}))
        return result

    def judge(goal, response, **kwargs):
        judged.append((response, kwargs.get("evidence")))
        return "done", "runtime evidence verified", False, None, False

    monkeypatch.setattr(goals, "judge_goal", judge)
    agent = SimpleNamespace(session_id=transcript_id, run_conversation=run_conversation, clear_interrupt=lambda: None)
    session = _turn_session(agent, session_key)
    server._run_prompt_submit("rid", "sid", session, "verify the work")
    successful = outcome in {"empty", "whitespace", "tool_only", "handoff"}
    after = goals.load_goal(session_key)
    assert after.turns_used == int(successful)
    assert after.status == ("done" if successful else "active")
    assert len(judged) == int(successful)
    if outcome == "tool_only":
        assert any("Artifact verified" in item["output"] for item in judged[0][1])
    loop_after = loops.load_loop(session_key)
    assert loop_after.awaiting_response is False
    assert loop_after.status == "active"
    assert loop_after.ticks_fired == 1
    completes = [payload for name, sid, payload in turn_env if name == "message.complete"]
    assert len(completes) == 1
    if outcome == "failed":
        assert completes[0]["status"] == "error"
        assert completes[0]["text"].strip()  # generated failure copy was not judged
    elif outcome == "interrupted":
        assert completes[0]["status"] == "interrupted"
    else:
        assert completes[0]["status"] == "complete"


@pytest.mark.parametrize("marker", ["[SILENT]", "NO_REPLY"])
def test_bot_chat_silence_reaches_real_goal_judge_and_loop_bookkeeping(server, turn_env, monkeypatch, marker):
    from types import SimpleNamespace
    from hermes_cli import goals, loops
    from tests.hermes_cli.test_goal_external_wait_backoff_real_path import _stub_judge
    from tests.tui_gateway.test_goal_command import _turn_session
    from tools.bot_mode_probe import BOT_CHAT_TITLE

    session_key = "bot-chat-silent-goal"
    goals.GoalManager(session_key).set("Wait for deployment health")
    loop = loops.LoopManager(session_key)
    loop.set("Check the deployment", until="deployment healthy")
    assert loop.fire_tick()
    # A second identical quiet tick must back off without invoking the loop's until judge.
    loop.state.last_response_digest = loops._digest_response(marker)
    loop._save()
    initial_delay = loop.state.current_delay
    calls, followups, receipts = [], [], []
    _stub_judge(monkeypatch, {"verdict": "continue", "reason": "still waiting"}, calls)
    monkeypatch.setattr(server, "_run_post_turn_followups", lambda *args: followups.append(args[-1]))
    result = {"completed": True, "final_response": marker}
    agent = SimpleNamespace(session_id=session_key, _session_title_hint=BOT_CHAT_TITLE,
                            run_conversation=lambda *a, **k: result, clear_interrupt=lambda: None)
    session = _turn_session(agent, session_key)
    server._run_prompt_submit("rid", "sid", session, "Check the deployment",
                              terminal_callback=receipts.append)

    after = goals.load_goal(session_key)
    assert len(calls) == 1  # real judge parses a stubbed auxiliary response, not an empty-text shortcut
    assert marker in str(calls[0]["messages"])
    assert after.turns_used == 1
    assert after.last_reason == "still waiting"
    # A silent automatic no-progress turn parks quietly on the first ladder rung; nothing clears.
    assert after.status == "active"
    assert after.backoff_level == 1
    assert after.waiting_seconds == goals._NO_PROGRESS_BACKOFF_S[0] == 900
    assert followups == [None]  # parked on the ladder: no goal continuation is dispatched
    loop_after = loops.load_loop(session_key)
    assert loop_after.awaiting_response is False
    assert loop_after.last_response_digest == loops._digest_response(marker)
    assert loop_after.current_delay == 15 * 60
    completes = [payload for name, sid, payload in turn_env if name == "message.complete"]
    assert len(completes) == 1 and completes[0]["text"] == ""
    assert receipts[0]["text"] == ""  # hosted-room delivery must not leak the preserved marker
    assert result["final_response"] == marker


def test_tui_exception_releases_inflight_loop_claim(server, turn_env, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import loops
    from tests.tui_gateway.test_goal_command import _turn_session

    session_key = "tui-exception-loop-claim"
    loop = loops.LoopManager(session_key)
    loop.set("Check the deployment", interval_seconds=300)
    assert loop.fire_tick()

    def run_conversation(*args, **kwargs):
        raise RuntimeError("dispatcher failure")

    agent = SimpleNamespace(session_id="tui-exception-transcript", run_conversation=run_conversation,
                            clear_interrupt=lambda: None)
    session = _turn_session(agent, session_key)
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    server._run_prompt_submit("rid", "sid", session, "verify the work")

    after = loops.load_loop(session_key)
    assert after is not None
    assert after.awaiting_response is False
    assert after.next_due_at > 0
    next_due_at = after.next_due_at
    monkeypatch.setattr(loops.time, "time", lambda: next_due_at + 1)
    next_loop = loops.LoopManager(session_key)
    assert next_loop.fire_tick()
    assert next_loop.state is not None
    assert next_loop.state.ticks_fired == after.ticks_fired + 1
