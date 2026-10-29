"""A successful empty or tool-only turn is judged from recorded evidence.

Regression: evaluation admitted empty successful turns, but ``judge_goal`` returned CONTINUE on
blank text before calling the judge, so tool evidence was discarded and the goal could never
complete. These tests patch only the auxiliary transport (``call_llm``), never ``judge_goal``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import goals
from hermes_cli.goals import GoalManager
from tests.hermes_cli.test_goal_judge_evidence import _record_tool_call, _session_db

PLACEHOLDER = goals.JUDGE_EMPTY_RESPONSE_PLACEHOLDER
DONE = '{"verdict":"done","reason":"artifact verified by recorded tool result"}'


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


@pytest.fixture
def transport(monkeypatch):
    import agent.auxiliary_client as aux

    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=DONE))])

    monkeypatch.setattr(aux, "call_llm", fake_call_llm)
    return calls


def _user_prompt(call):
    return next(m["content"] for m in call["messages"] if m["role"] == "user")


EVIDENCE = [{"tool": "terminal", "call": "$ ./bin/check", "timestamp": 0.0, "output": "Artifact verified"}]


@pytest.mark.parametrize("text", ["", "   \n"])
def test_blank_response_with_evidence_reaches_the_judge(transport, text):
    verdict, reason, parse_failed, _wait, transport_failed = goals.judge_goal(
        "Verify the artifact", text, evidence=EVIDENCE)

    assert (verdict, parse_failed, transport_failed) == ("done", False, False)
    assert "recorded tool result" in reason
    assert len(transport) == 1
    prompt = _user_prompt(transport[0])
    assert "./bin/check" in prompt and "Artifact verified" in prompt
    assert PLACEHOLDER in prompt


def test_blank_response_with_visible_report_reaches_the_judge(transport):
    verdict, *_ = goals.judge_goal("Verify the artifact", "", last_visible_report="Verified: 12 passed")

    assert verdict == "done"
    assert "Verified: 12 passed" in _user_prompt(transport[0])


@pytest.mark.parametrize("kwargs", [{}, {"evidence": []}, {"citations": {"cited": [], "unresolved": []}},
                                    {"last_visible_report": "  "}])
def test_blank_response_without_evidence_short_circuits(transport, kwargs):
    verdict, reason, parse_failed, _wait, transport_failed = goals.judge_goal("Verify the artifact", " ", **kwargs)

    assert (verdict, parse_failed, transport_failed) == ("continue", False, False)
    assert reason == "empty response (nothing to evaluate)"
    assert transport == []


def test_text_response_prompt_has_no_placeholder(transport):
    goals.judge_goal("Verify the artifact", "Done: artifact verified", evidence=EVIDENCE)

    prompt = _user_prompt(transport[0])
    assert "Done: artifact verified" in prompt
    assert PLACEHOLDER not in prompt


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_successful_turn_with_tool_result_completes_goal(hermes_home, transport, text):
    sid = "empty-turn-e2e"
    db = _session_db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Verify the artifact")
    _record_tool_call(db, sid, "terminal", {"command": "./bin/check"}, "Artifact verified", call_id="tool-1")

    decision = mgr.evaluate_after_turn(text)

    assert decision["verdict"] == "done"
    assert decision["should_continue"] is False
    assert goals.load_goal(sid).status == "done"
    assert len(transport) == 1
    prompt = _user_prompt(transport[0])
    assert "./bin/check" in prompt and "Artifact verified" in prompt
    assert PLACEHOLDER in prompt


def test_empty_successful_turn_without_evidence_does_not_call_judge(hermes_home, transport):
    sid = "empty-turn-no-evidence"
    _session_db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Verify the artifact")

    decision = mgr.evaluate_after_turn("")

    assert decision["verdict"] == "continue"
    assert goals.load_goal(sid).status == "active"
    assert transport == []


def test_evidence_free_callers_keep_blank_short_circuit(transport):
    """loops.py, kanban.py and the bare-text fallback pass no evidence: blank text is still skipped."""
    assert goals.judge_goal("deployment healthy", "")[:2] == ("continue", "empty response (nothing to evaluate)")
    assert transport == []
