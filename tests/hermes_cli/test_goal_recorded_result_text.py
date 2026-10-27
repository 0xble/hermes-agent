"""Recorded completion proof is text, not the JSON escaping of that text."""

import json
from pathlib import Path

import pytest

from hermes_cli import goals


CRITERION = "The recorded continuation timestamp is positive"
PROOF = "native automatic-continuation timestamp: 1234.5"
SESSION = "recorded-result-proof"


@pytest.fixture
def recorded_session(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("goals:\n  autonomy: true\n", encoding="utf-8")
    goals._DB_CACHE.clear()
    db = goals._get_session_db()
    assert db is not None
    db.ensure_session(SESSION, source="cli")
    yield db
    goals._DB_CACHE.clear()


def _goal():
    manager = goals.GoalManager(SESSION)
    manager.set("Verify recorded result text", contract=goals.GoalContract(
        verification=CRITERION, constraints="Do not send messages"))
    return manager


@pytest.mark.parametrize("output,proof", [
    pytest.param(PROOF, PROOF, id="plain-text"),
    pytest.param(json.dumps({"output": "diagnostic line\n" + PROOF,
                             "exit_code": 0, "error": None}), PROOF, id="terminal-newline"),
    pytest.param(json.dumps({"output": "diagnostic line\r\n\t" + PROOF}), PROOF,
                 id="terminal-crlf-tab"),
    pytest.param(json.dumps({"content": [{"type": "text", "text": "diagnostic line\n" + PROOF}]}),
                 PROOF, id="nested-result"),
    pytest.param(json.dumps({"output": 'release readback: "abc1234"'}),
                 'release readback: "abc1234"', id="escaped-quotes"),
    pytest.param(json.dumps({"output": "release caf\u00e9: abc1234"}),
                 "release caf\u00e9: abc1234", id="escaped-unicode"),
    pytest.param(json.dumps({"output": r"artifact path: C:\build\abc1234"}),
                 r"artifact path: C:\build\abc1234", id="escaped-backslash"),
])
def test_completion_matches_recorded_result_text(recorded_session, output, proof):
    manager = _goal()
    recorded_session.append_message(SESSION, "tool", output, tool_name="terminal")
    result = manager.complete(reason="Measured result recorded", evidence={CRITERION: proof})
    assert result["ok"], result
    persisted = goals.load_goal(SESSION)
    assert persisted is not None
    assert persisted.status == "done"
    assert persisted.contract.constraints == "Do not send messages"
    assert persisted.revisions[-1]["evidence"] == {CRITERION: proof}


@pytest.mark.parametrize("case,output,role,tool", [
    pytest.param("partial", json.dumps({"output": "diagnostic\nx" + PROOF + "suffix"}),
                 "tool", "terminal", id="partial-token"),
    pytest.param("negated", json.dumps({"output": "diagnostic\nno " + PROOF}),
                 "tool", "terminal", id="negated-output"),
    pytest.param("negated", json.dumps({"summary": "no evidence for", "output": PROOF}),
                 "tool", "terminal", id="negative-envelope-context"),
    pytest.param("negated", json.dumps({"summary": "no. ", "output": PROOF}).replace(
        "no.", r"no\u002e"), "tool", "terminal", id="raw-negative-context-veto"),
    pytest.param("negated", json.dumps({"output": "no " + PROOF}).replace(
        "no ", r"\u006e\u006f "), "tool", "terminal", id="escaped-negation"),
    pytest.param("stale", json.dumps({"output": "diagnostic\n" + PROOF}),
                 "tool", "terminal", id="previous-goal-result"),
    pytest.param("excluded", json.dumps({"output": "diagnostic\n" + PROOF}),
                 "tool", "goal_set", id="bookkeeping-result"),
    pytest.param("assistant", json.dumps({"output": "diagnostic\n" + PROOF}),
                 "assistant", None, id="assistant-prose"),
    pytest.param("fabricated", json.dumps({"output": "diagnostic\nno measured timestamp"}),
                 "tool", "terminal", id="absent-proof"),
    pytest.param("literal", json.dumps({"output": r"diagnostic\n" + PROOF}),
                 "tool", "terminal", id="literal-backslash-not-newline"),
    pytest.param("malformed", r'{"output":"diagnostic\n' + PROOF,
                 "tool", "terminal", id="malformed-json-is-plain-text"),
    pytest.param("separate", json.dumps({"before": "native automatic-continuation",
                                        "after": "timestamp: 1234.5"}),
                 "tool", "terminal", id="distinct-fields-stay-distinct"),
    pytest.param("foreign", json.dumps({"output": "diagnostic\n" + PROOF}),
                 "tool", "terminal", id="different-session-result"),
])
def test_result_decoding_preserves_evidence_refusals(recorded_session, case, output, role, tool):
    if case == "stale":
        recorded_session.append_message(SESSION, role, output, tool_name=tool)
    manager = _goal()
    if case == "foreign":
        recorded_session.ensure_session("different-result-session", source="cli")
        recorded_session.append_message("different-result-session", role, output, tool_name=tool)
    elif case != "stale":
        recorded_session.append_message(SESSION, role, output, tool_name=tool)
    result = manager.complete(reason="Attempt recorded completion", evidence={CRITERION: PROOF})
    assert not result["ok"], result
    assert result["error_code"] == "completion_unproven"
    persisted = goals.load_goal(SESSION)
    assert persisted is not None
    assert persisted.status == "active"
    if case == "negated":
        assert "negated" in result["error"]
