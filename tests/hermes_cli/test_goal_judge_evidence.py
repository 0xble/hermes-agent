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
    _record_tool_call(db, sid, "goal_set", {"action": "status"}, "goal state", call_id="goal")
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


def test_unchanged_done_repeat_is_explicitly_silent():
    assert goals.SILENCE_MARKER in goals._AUTONOMOUS_REPLY_RULES
    assert "FIRST time" in goals._AUTONOMOUS_REPLY_RULES
    assert "nothing material changed" in goals._AUTONOMOUS_REPLY_RULES
    assert "still require a visible reply" not in goals._AUTONOMOUS_REPLY_RULES


def test_silent_repeat_still_rejudges_and_can_finish_from_evidence(hermes_home, monkeypatch):
    sid = "silent-repeat-rejudge"
    db = _session_db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Write the systems wiki", contract=GoalContract(
        outcome="PR open with every page", verification="wiki-check passes on the PR head"))
    _record_tool_call(db, sid, "terminal", {"command": "gh pr checks 197 -R 0xble/Workspace"},
                      PR_CHECK_OUTPUT, call_id="silent-call")
    prompts = _capture_prompts(monkeypatch, ['{"verdict":"done","reason":"recorded proof is sufficient"}'])

    decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)

    assert decision["verdict"] == "done"
    assert len(prompts) == 1
    assert goals.SILENCE_MARKER in prompts[0]
    assert "wiki-check\\tpass" in prompts[0]


@pytest.mark.parametrize("mode", ["plain", "contract", "subgoals"])
@pytest.mark.parametrize("report", [
    "Done. The wiki is published.",
    "✅ Done. The wiki is published.",
    "Installed and done.",
    "## Completion\n- ✅ Done. The wiki is published.",
])
def test_silent_recheck_passes_newest_visible_report_with_older_citations(hermes_home, monkeypatch, mode, report):
    sid = "last-visible-report"
    db = _session_db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Publish the verified wiki", max_turns=0, contract=(GoalContract(
        outcome="Wiki published", verification="wiki-check passes on the published head") if mode == "contract" else None))
    if mode == "subgoals":
        mgr.add_subgoal("wiki-check passes on the published head")
    report += " Evidence: `gh pr checks 197 -R 0xble/Workspace`."
    _record_tool_call(db, sid, "terminal", {"command": "gh pr checks 197 -R 0xble/Workspace"},
                      PR_CHECK_OUTPUT, call_id="report-proof")
    db.append_message(sid, "assistant", report)
    with patch.object(goals, "judge_goal", return_value=PLAIN_CONTINUE):
        mgr.evaluate_after_turn(report)
    for i in range(goals._EVIDENCE_MAX_ENTRIES + 2):
        _record_tool_call(db, sid, "read_file", {"path": f"notes-{i}"}, f"note {i}", call_id=f"note-{i}")
    assert all("wiki-check" not in row["output"] for row in goals.collect_goal_evidence(sid))
    # Report and proof survive a database reopen and repeated silent judgments.
    db.close()
    goals._DB_CACHE.clear()
    seen = []
    replies = iter(['{"verdict":"continue","reason":"check again"}',
                    '{"verdict":"done","reason":"report and recorded proof satisfy the goal"}'])

    def judge(_call, system, prompt, _timeout):
        seen.append((system, prompt))
        return next(replies)

    monkeypatch.setattr(goals, "_call_goal_judge_llm", judge)
    mgr = GoalManager(session_id=sid)
    first = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    assert first["verdict"] == "continue"
    second = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    assert second["verdict"] == "done"
    assert mgr.state.status == "done"
    assert len(seen) == 2
    assert seen[0][0] == seen[1][0]
    for system, prompt in seen:
        assert "Decide whether that report claims completion" in system
        assert report not in system
        assert "Agent's last visible report from this goal's lifetime" in prompt
        assert report in prompt
        assert "wiki-check\\tpass" in prompt
        assert "Evidence the response cites, located verbatim" in prompt


def test_silent_recheck_newer_failed_report_replaces_done_and_its_citations(hermes_home, monkeypatch):
    sid = "visible-report-lifetime"
    db = _session_db(sid)
    mgr = GoalManager(sid)
    state = mgr.set("Publish the wiki", max_turns=0)
    db.append_message(sid, "assistant", "Done. Previous goal: `old-proof-111111`.", timestamp=state.created_at - 1)
    _record_tool_call(db, sid, "terminal", {"command": "wiki-check"},
                      "proof-222222: wiki-check pass", call_id="old-proof")
    old = "Done. Evidence: `proof-222222`."
    db.append_message(sid, "assistant", old)
    latest = "The verification failed; I need to fix it. Evidence: `failure-333333`."
    _record_tool_call(db, sid, "terminal", {"command": "wiki-check"},
                      "failure-333333: wiki-check failed", call_id="failed-proof")
    db.append_message(sid, "assistant", latest)
    db.append_message(sid, "assistant", "Done. Hidden row: `proof-444444`.", display_kind="hidden")
    db.append_message(sid, "assistant", "Done. Compression summary: `proof-555555`.", _compressed_summary=True)
    db.append_message(sid, "assistant", "Done. Tool-call commentary.", tool_calls=[{
        "id": "commentary", "function": {"name": "terminal", "arguments": "{}"},
    }])
    db.append_message(sid, "assistant", goals.SILENCE_MARKER)
    for i in range(goals._EVIDENCE_MAX_ENTRIES + 2):
        _record_tool_call(db, sid, "read_file", {"path": f"notes-{i}"}, f"note {i}", call_id=f"note-{i}")
    prompts = _capture_prompts(monkeypatch, ['{"verdict":"continue","reason":"verification failed"}'])
    decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    assert decision["verdict"] == "continue"
    assert latest in prompts[0]
    assert "failure-333333: wiki-check failed" in prompts[0]
    for excluded in (old, "old-proof-111111", "proof-222222", "proof-444444", "proof-555555", "Tool-call commentary"):
        assert excluded not in prompts[0]


def test_silent_recheck_without_visible_report_keeps_original_judge_input(hermes_home, monkeypatch):
    sid = "no-visible-report"
    db = _session_db(sid)
    mgr = GoalManager(sid)
    state = mgr.set("Publish the wiki", max_turns=0)
    db.append_message(sid, "assistant", "Done. Old goal: `old-proof-123456`.", timestamp=state.created_at - 1)
    db.append_message(sid, "assistant", "Done. Hidden report.", display_kind="hidden")
    db.append_message(sid, "assistant", goals.SILENCE_MARKER)
    prompts = _capture_prompts(monkeypatch, ['{"verdict":"continue","reason":"r"}'] * 2)
    monkeypatch.setattr(goals, "safe_strftime", lambda *_args: "fixed current time")
    goals.judge_goal(state.goal, goals.SILENCE_MARKER)
    mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    assert prompts[0] == prompts[1]
    assert "Agent's last visible report" not in prompts[1]


def test_silent_recheck_bounds_report_without_losing_cited_proof(hermes_home, monkeypatch):
    sid = "bounded-visible-report"
    db = _session_db(sid)
    mgr = GoalManager(sid)
    mgr.set("Publish the wiki", max_turns=0)
    head = goals._JUDGE_RESPONSE_SNIPPET_CHARS
    tail = goals._JUDGE_RESPONSE_TAIL_CHARS
    citation = "bounded-proof-123456"
    report = "## ✅ Done\n" + "x" * head + f" Evidence: `{citation}`. " + "y" * tail + "\nWiki published."
    _record_tool_call(db, sid, "terminal", {"command": "wiki-check"},
                      f"{citation}: wiki-check pass", call_id="bounded-proof")
    db.append_message(sid, "assistant", report)
    for i in range(goals._EVIDENCE_MAX_ENTRIES + 2):
        _record_tool_call(db, sid, "read_file", {"path": f"notes-{i}"}, f"note {i}", call_id=f"note-{i}")
    prompts = _capture_prompts(monkeypatch, ['{"verdict":"done","reason":"recorded proof"}'])
    decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    assert decision["verdict"] == "done"
    rendered = prompts[0].split("(newest final report; agent prose, not proof):\n", 1)[1].split("\n\n", 1)[0]
    assert rendered.startswith(report[:head])
    assert rendered.endswith(report[-tail:])
    assert "chars omitted" in rendered
    assert len(rendered) <= head + tail + 64
    assert citation not in rendered
    assert f"{citation}: wiki-check pass" in prompts[0]


@pytest.mark.parametrize("interrupt", ["evidence", "visible", "user-judge-failure", "user-silent"])
def test_silent_stall_resets_only_for_real_breaks_in_silence(hermes_home, interrupt):
    sid = "silent-stall-reset"
    db = _session_db(sid)
    mgr = GoalManager(sid)
    mgr.set("Publish the wiki", max_turns=0)
    limit = goals.DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS
    with patch.object(goals, "judge_goal", return_value=PLAIN_CONTINUE):
        for _ in range(limit - 1):
            mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    assert mgr.state.consecutive_silent_no_progress == limit - 1
    reply = goals.SILENCE_MARKER
    verdict = PLAIN_CONTINUE
    if interrupt == "evidence":
        _record_tool_call(db, sid, "terminal", {"command": "wiki-check"}, "new result", call_id="fresh")
    elif interrupt == "visible":
        reply = "Still working; the verification step changed."
    elif interrupt == "user-judge-failure":
        verdict = ("continue", "judge unavailable", False, None, True)
    with patch.object(goals, "judge_goal", return_value=verdict):
        mgr.evaluate_after_turn(reply, user_initiated=interrupt in {"user-judge-failure", "user-silent"})
    assert mgr.state.consecutive_silent_no_progress == 0
    assert mgr.state.status == "active"
    # A visible autonomous reply breaks silence, but may still enter the independent ordinary
    # no-progress backoff. Lift that hold before the next judged silent recheck.
    mgr.stop_waiting()
    # Existing visible dispute counts must not force silent turns to pause early.
    mgr.state.consecutive_disputes = goals.DEFAULT_MAX_CONSECUTIVE_DISPUTES - 1
    mgr._save()
    mgr = GoalManager(sid)
    with patch.object(goals, "judge_goal", return_value=DISPUTED):
        decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    assert decision["status"] == "active"  # ordinary no-progress backoff is not a dispute pause
    assert mgr.state.consecutive_silent_no_progress == 1
    assert mgr.state.status == "active"
    assert decision["verdict"] in {"continue", "wait"}


def test_gate_failure_between_silent_rechecks_resets_streak(hermes_home):
    sid = "silent-stall-gate-reset"
    mgr = GoalManager(session_id=sid)
    mgr.set("Publish the wiki", max_turns=0)
    mgr.add_gate("wiki-check")
    gate_results = iter([
        (True, 0, "pass"),
        (True, 0, "pass"),
        (False, 1, "still failing"),
        (True, 0, "pass"),
    ])
    with patch.object(goals, "run_gate", side_effect=lambda *_args, **_kwargs: next(gate_results)), \
            patch.object(goals, "judge_goal", return_value=PLAIN_CONTINUE):
        mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
        mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
        failed = mgr.evaluate_after_turn("The user-visible turn is still working.")
        resumed = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)

    assert failed["verdict"] == "gate_failed"
    assert resumed["status"] == "active"
    assert resumed["should_continue"] is False
    assert resumed["verdict"] == "wait"
    assert mgr.state.consecutive_silent_no_progress == 1




@pytest.mark.parametrize("interrupt", ["user-silent", "visible", "evidence"])
@pytest.mark.parametrize("early_exit", ["gate", "timed-wait"])
def test_real_break_resets_before_nonjudging_exit(hermes_home, interrupt, early_exit):
    sid = f"silent-entry-reset-{interrupt}-{early_exit}"
    db = _session_db(sid)
    mgr = GoalManager(sid)
    mgr.set("Publish the wiki", max_turns=0)
    with patch.object(goals, "judge_goal", return_value=PLAIN_CONTINUE):
        for _ in range(2):
            mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    assert mgr.state.consecutive_silent_no_progress == 2
    if early_exit == "gate":
        mgr.add_gate("wiki-check")
    else:
        mgr.wait_for_seconds(60, reason="external check pending")
    if interrupt == "evidence":
        _record_tool_call(db, sid, "write_file", {"path": "wiki.md"}, "saved", call_id="new-action")
    reply = "Verification changed." if interrupt == "visible" else goals.SILENCE_MARKER
    with patch.object(goals, "judge_goal") as judge, \
            patch.object(goals, "run_gate", return_value=(False, 1, "still failing")):
        mgr.evaluate_after_turn(reply, user_initiated=interrupt == "user-silent")
    judge.assert_not_called()
    assert GoalManager(sid).state.consecutive_silent_no_progress == 0
    if early_exit == "timed-wait":
        mgr.stop_waiting()
    with patch.object(goals, "judge_goal", return_value=PLAIN_CONTINUE), \
            patch.object(goals, "run_gate", return_value=(True, 0, "passed")):
        resumed = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    assert resumed["status"] == "active"
    # A held wait did not complete a turn: its new evidence is still current when the judge resumes.
    expected = 0 if interrupt == "evidence" and early_exit == "timed-wait" else 1
    assert GoalManager(sid).state.consecutive_silent_no_progress == expected


@pytest.mark.parametrize(
    "early_return",
    [
        "quality_gate",
        "judge_wait",
        "parked",
        "live_blocked",
        "live_parked",
        "blocked",
        "done",
        "transport_error",
        "parse_error",
        "silent_stall",
        "dispute",
        "max_turns",
        "delegation",
        "no_progress",
        "continuation_gap",
    ],
)
def test_post_turn_exit_classifies_silent_streak_by_progress_and_judgment(hermes_home, early_return):
    sid = f"silent-stall-early-{early_return}"
    mgr = GoalManager(session_id=sid)
    mgr.set("Publish the wiki", max_turns=0)
    with patch.object(goals, "judge_goal", return_value=PLAIN_CONTINUE):
        mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    assert mgr.state.consecutive_silent_no_progress == 1
    mgr.state.consecutive_no_progress = 0
    mgr.state.backoff_level = 0
    mgr._save()

    decision = None
    if early_return == "quality_gate":
        mgr.add_gate("wiki-check")
        with patch.object(goals, "run_gate", return_value=(False, 1, "still failing")):
            decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    elif early_return == "judge_wait":
        wait = ("wait", "waiting for the check", False, {"seconds": 1}, False)
        with patch.object(goals, "judge_goal", return_value=wait):
            decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    elif early_return == "parked":
        mgr.wait_for_seconds(60, reason="parked test")
        decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    elif early_return == "live_blocked":
        mgr.state.waiting_on_pid = 1
        mgr.state.waiting_since = time.time()
        mgr.state.waiting_reason = "live blocker"
        mgr._save()
        with patch.object(mgr, "_stage_live_barrier", return_value=(False, None, "blocked")):
            decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    elif early_return == "live_parked":
        mgr.state.waiting_on_pid = 1
        mgr.state.waiting_since = time.time()
        mgr.state.waiting_reason = "live wait"
        mgr._save()
        with patch.object(mgr, "_stage_live_barrier", return_value=(True, None, None)):
            decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    elif early_return == "blocked":
        blocked = ("blocked", "needs user input", False, None, False)
        with patch.object(goals, "judge_goal", return_value=blocked):
            decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    elif early_return == "done":
        done = ("done", "finished", False, None, False)
        with patch.object(goals, "judge_goal", return_value=done):
            decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    elif early_return == "transport_error":
        mgr.state.consecutive_transport_failures = goals.DEFAULT_MAX_CONSECUTIVE_TRANSPORT_FAILURES - 1
        mgr._save()
        failure = ("continue", "judge unavailable", False, None, True)
        with patch.object(goals, "judge_goal", return_value=failure):
            decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    elif early_return == "parse_error":
        mgr.state.consecutive_parse_failures = goals.DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES - 1
        mgr._save()
        failure = ("continue", "unparseable judge output", True, None, False)
        with patch.object(goals, "judge_goal", return_value=failure):
            decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    elif early_return == "silent_stall":
        mgr.state.consecutive_silent_no_progress = goals.DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS - 1
        mgr._save()
        with patch.object(goals, "judge_goal", return_value=PLAIN_CONTINUE):
            decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    elif early_return == "dispute":
        mgr.state.consecutive_disputes = goals.DEFAULT_MAX_CONSECUTIVE_DISPUTES - 1
        mgr._save()
        with patch.object(goals, "judge_goal", return_value=DISPUTED):
            decision = mgr.evaluate_after_turn("Done, but I need review.")
    elif early_return == "max_turns":
        mgr.state.max_turns = mgr.state.turns_used + 1
        mgr._save()
        with patch.object(goals, "judge_goal", return_value=PLAIN_CONTINUE):
            decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    elif early_return == "delegation":
        with patch.object(goals, "judge_goal", return_value=PLAIN_CONTINUE):
            decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False, active_delegations=1)
    elif early_return == "no_progress":
        mgr.state.consecutive_no_progress = goals.DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS - 1
        mgr._save()
        with patch.object(goals, "judge_goal", return_value=PLAIN_CONTINUE):
            decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
    elif early_return == "continuation_gap":
        mgr.min_continuation_gap_seconds = 60
        mgr.state.last_continuation_at = time.time()
        mgr._save()
        with patch.object(goals, "judge_goal", return_value=PLAIN_CONTINUE):
            decision = mgr.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)

    assert decision is not None
    expected = {
        "blocked": 0, "done": 0, "dispute": 0,  # visible disputed user turn
        "silent_stall": goals.DEFAULT_MAX_CONSECUTIVE_NO_PROGRESS,
        "max_turns": 2, "delegation": 2, "no_progress": 2, "continuation_gap": 2,
    }.get(early_return, 1)
    assert GoalManager(sid).state.consecutive_silent_no_progress == expected


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

    limit = goals.DEFAULT_MAX_CONSECUTIVE_DISPUTES
    with patch.object(goals, "judge_goal", side_effect=_judge_sequence(*[DISPUTED] * limit)):
        decisions = [mgr.evaluate_after_turn(CLAIM_ONLY_REPLY) for _ in range(limit)]

    assert all(d["should_continue"] for d in decisions[:-1])
    last = decisions[-1]
    assert last["should_continue"] is False
    assert last["verdict"] == "disputed"
    assert "/goal clear" in last["message"] and "/goal resume" in last["message"]
    persisted = load_goal(sid)
    assert persisted.status == "paused"
    assert persisted.paused_reason.startswith(goals._DISPUTED_PAUSE_PREFIX)
    assert persisted.turns_used == limit


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
    limit = goals.DEFAULT_MAX_CONSECUTIVE_DISPUTES
    with patch.object(goals, "judge_goal", side_effect=_judge_sequence(*[DISPUTED] * limit)):
        for _ in range(limit):
            mgr.evaluate_after_turn("done")
    assert mgr.state.status == "paused"
    assert mgr.resume_for_user_input() is False
    assert mgr.state.status == "paused"


def test_legacy_state_without_dispute_counter_loads():
    legacy = json.dumps({"goal": "g", "status": "active", "turns_used": 3})
    assert goals.GoalState.from_json(legacy).consecutive_disputes == 0
