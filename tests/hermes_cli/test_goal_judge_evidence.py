"""The goal judge sees recorded tool evidence, and a disputed completion claim stops the loop.

Regression source: a contract goal whose agent verified its PR with ``gh pr checks`` (a tool call)
and then summarized the result in prose. The judge only saw the prose, returned CONTINUE for
"no concrete evidence" four times in 46 seconds, and nothing bounded the loop short of the turn
budget.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from hermes_cli import goals
from hermes_cli.goals import GoalContract, GoalManager, load_goal


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _record_tool_call(db, session_id, name, arguments, output, *, call_id):
    db.append_message(session_id, "assistant", "", tool_calls=[{
        "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)},
    }])
    db.append_message(session_id, "tool", output, tool_name=name, tool_call_id=call_id)


def _session_db(session_id):
    db = goals._get_session_db()
    assert db is not None
    db.ensure_session(session_id, source="telegram")
    return db


PR_CHECK_OUTPUT = json.dumps({
    "output": "OPEN\t\t25b9027ab70c3254b7c361862ed91557eb27dbd6\t66\nwiki-check\tpass", "exit_code": 0,
})
CLAIM_ONLY_REPLY = (
    "The work is done and I've stopped. PR #197 is still open and unmerged, with all 66 files. "
    "wiki-check passes on head 25b9027. Nothing more happens until you review it."
)


def _capture_prompts(monkeypatch, replies):
    prompts = []
    replies = iter(replies)

    def capture(_call, _system, prompt, _timeout):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(goals, "_call_goal_judge_llm", capture)
    return prompts


# ── evidence ledger ──────────────────────────────────────────────────


def test_judge_sees_tool_output_the_reply_only_summarized(hermes_home, monkeypatch):
    sid = "evidence-replay"
    db = _session_db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Write the systems wiki", contract=GoalContract(
        outcome="PR open with every page", verification="wiki-check passes on the PR head"))
    _record_tool_call(db, sid, "terminal", {"command": "gh pr checks 197 -R 0xble/Workspace"},
                      PR_CHECK_OUTPUT, call_id="call-1")
    prompts = _capture_prompts(monkeypatch, ['{"verdict":"done","reason":"wiki-check pass recorded"}'])

    decision = mgr.evaluate_after_turn(CLAIM_ONLY_REPLY)

    assert decision["verdict"] == "done"
    assert "gh pr checks 197" in prompts[0]
    assert "wiki-check\\tpass" in prompts[0]
    assert "authoritative, not written by the agent" in prompts[0]


def test_evidence_excludes_bookkeeping_tools_and_pre_goal_results(hermes_home):
    sid = "evidence-filter"
    db = _session_db(sid)
    _record_tool_call(db, sid, "terminal", {"command": "echo before-goal"}, "before-goal", call_id="old")
    time.sleep(0.01)
    since = time.time()
    time.sleep(0.01)
    _record_tool_call(db, sid, "skill_view", {"name": "hermes"}, "skill body", call_id="skill")
    _record_tool_call(db, sid, "hindsight_recall", {"query": "x"}, "memories", call_id="mem")
    _record_tool_call(db, sid, "terminal", {"command": "./bin/check"}, "90 passed", call_id="check")

    evidence = goals.collect_goal_evidence(sid, since=since)

    assert [e["tool"] for e in evidence] == ["terminal"]
    assert "./bin/check" in evidence[0]["call"]
    assert evidence[0]["output"] == "90 passed"


def test_evidence_keeps_output_tail_bounded_and_redacted(hermes_home):
    sid = "evidence-bounds"
    db = _session_db(sid)
    secret = "ghp_" + "a" * 36
    long_output = "noise " * 2000 + f"token={secret}\nRESULT: 12 passed, exit 0"
    _record_tool_call(db, sid, "terminal", {"command": f"GH_TOKEN={secret} make test"}, long_output, call_id="c")
    for i in range(goals._EVIDENCE_MAX_ENTRIES + 3):
        _record_tool_call(db, sid, "read_file", {"path": f"f{i}"}, f"content {i}", call_id=f"r{i}")

    newest = goals.collect_goal_evidence(sid)
    assert len(newest) == goals._EVIDENCE_MAX_ENTRIES
    assert newest[-1]["output"] == f"content {goals._EVIDENCE_MAX_ENTRIES + 2}"

    [entry] = goals.collect_goal_evidence(sid, max_entries=goals._EVIDENCE_MAX_ENTRIES + 4)[:1]
    assert entry["output"].endswith("RESULT: 12 passed, exit 0")
    assert len(entry["output"]) <= goals._EVIDENCE_OUTPUT_CHARS + 8
    assert secret not in entry["output"] and secret not in entry["call"]


def test_prompt_without_evidence_is_unchanged(monkeypatch):
    prompts = _capture_prompts(monkeypatch, ['{"verdict":"continue","reason":"r"}'] * 2)
    goals.judge_goal("Finish the work", "did a thing", timeout=1)
    goals.judge_goal("Finish the work", "did a thing", evidence=[], timeout=1)
    assert prompts[0] == prompts[1]
    assert "Tool results recorded" not in prompts[0]


def test_passing_gate_is_shown_to_judge_as_evidence(hermes_home, monkeypatch):
    sid = "evidence-gate"
    mgr = GoalManager(session_id=sid)
    mgr.set("ship it")
    mgr.add_gate("true")
    prompts = _capture_prompts(monkeypatch, ['{"verdict":"done","reason":"gate passed"}'])

    with patch.object(goals, "run_gate", return_value=(True, 0, "all 4 checks green")):
        decision = mgr.evaluate_after_turn("shipped")

    assert decision["verdict"] == "done"
    assert "quality gate" in prompts[0] and "$ true" in prompts[0]
    assert "exit 0 (passed)" in prompts[0] and "all 4 checks green" in prompts[0]


def test_evidence_failure_falls_back_to_response_only(hermes_home, monkeypatch):
    mgr = GoalManager(session_id="evidence-broken")
    mgr.set("ship it")
    monkeypatch.setattr(goals, "_get_session_db", lambda: None)
    assert goals.collect_goal_evidence("evidence-broken") == []


# ── dispute stall breaker ────────────────────────────────────────────


def test_parse_marks_only_explicit_disputes():
    parse = goals._parse_judge_response
    assert parse('{"verdict":"continue","disputed":true,"reason":"x"}')[3] == {"disputed": True}
    assert parse('{"verdict":"continue","disputed":"true","reason":"x"}')[3] == {"disputed": True}
    assert parse('{"verdict":"continue","disputed":false,"reason":"x"}')[3] is None
    assert parse('{"verdict":"continue","reason":"x"}')[3] is None
    assert parse('{"verdict":"done","disputed":true,"reason":"x"}')[3] is None


def _judge_sequence(*verdicts):
    results = iter(verdicts)
    return lambda *a, **k: next(results)


DISPUTED = ("continue", "no command output shown", False, {"disputed": True}, False)
PLAIN_CONTINUE = ("continue", "tests still missing", False, None, False)


def test_repeated_disputed_completion_pauses_for_the_user(hermes_home):
    sid = "dispute-pause"
    mgr = GoalManager(session_id=sid)
    mgr.set("Write the systems wiki", max_turns=100)

    with patch.object(goals, "judge_goal", side_effect=_judge_sequence(DISPUTED, DISPUTED)):
        first = mgr.evaluate_after_turn(CLAIM_ONLY_REPLY)
        second = mgr.evaluate_after_turn(CLAIM_ONLY_REPLY)

    assert first["should_continue"] is True
    assert second["should_continue"] is False
    assert second["verdict"] == "disputed"
    assert "/goal clear" in second["message"] and "/goal resume" in second["message"]
    persisted = load_goal(sid)
    assert persisted.status == "paused"
    assert persisted.paused_reason.startswith(goals._DISPUTED_PAUSE_PREFIX)
    assert persisted.turns_used == 2


def test_dispute_streak_resets_on_other_verdicts_and_resume(hermes_home):
    mgr = GoalManager(session_id="dispute-reset")
    mgr.set("ship it", max_turns=100)

    with patch.object(goals, "judge_goal", side_effect=_judge_sequence(DISPUTED, PLAIN_CONTINUE, DISPUTED)):
        for _ in range(3):
            decision = mgr.evaluate_after_turn("working")
    assert decision["should_continue"] is True
    assert mgr.state.consecutive_disputes == 1

    mgr.pause()
    mgr.resume()
    assert mgr.state.consecutive_disputes == 0


def test_disputed_pause_is_not_revived_by_ordinary_user_input(hermes_home):
    mgr = GoalManager(session_id="dispute-no-revive")
    mgr.set("ship it", max_turns=100)
    with patch.object(goals, "judge_goal", side_effect=_judge_sequence(DISPUTED, DISPUTED)):
        mgr.evaluate_after_turn("done")
        mgr.evaluate_after_turn("done")
    assert mgr.resume_for_user_input() is False
    assert mgr.state.status == "paused"


def test_legacy_state_without_dispute_counter_loads():
    legacy = json.dumps({"goal": "g", "status": "active", "turns_used": 3})
    assert goals.GoalState.from_json(legacy).consecutive_disputes == 0
