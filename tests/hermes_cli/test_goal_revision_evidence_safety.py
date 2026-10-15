"""Recorded output is provenance, not authority to retire a verification requirement."""

import pytest

from hermes_cli.goals import GoalContract, GoalManager, load_goal
from tests.hermes_cli.test_goal_adaptive import _db, _tool, hermes_home  # noqa: F401


pytestmark = pytest.mark.usefixtures("hermes_home")


@pytest.mark.parametrize("output", [
    "Security audit failed: 2 critical issues.",
    "Security audit passes: no critical issues.",
    "Security audit is obsolete: signed release supersedes it.",
    "Security audit is impossible: the scanner is unavailable.",
])
@pytest.mark.parametrize("verification", ["", "Quick smoke test passes."])
def test_untyped_evidence_cannot_retire_or_weaken_a_check(output, verification):
    sid = "revision-evidence-safety"
    db = _db(sid)
    mgr = GoalManager(sid)
    mgr.set("Ship X", contract=GoalContract(verification="Security audit passes."))
    _tool(db, sid, "terminal", {"command": "audit"}, output, "audit")
    before = load_goal(sid).to_json()

    result = mgr.revise(reason="retire the audit", contract={"verification": verification}, evidence=output)

    assert not result["ok"]
    assert result["error_code"] == "evidence_cannot_retire_verification"
    assert load_goal(sid).to_json() == before
    assert mgr.state.to_json() == before
    assert mgr.is_active()


def test_evidence_backed_replacement_keeps_equivalent_check_and_strengthens_contract():
    sid = "revision-evidence-retained"
    db = _db(sid)
    mgr = GoalManager(sid)
    mgr.set("Ship X", contract=GoalContract(verification="Security audit passes."))
    output = "Security audit failed: 2 critical issues. Smoke test is also required."
    _tool(db, sid, "terminal", {"command": "audit"}, output, "audit")
    replacement = "Security   audit passes.\nSmoke test passes."

    result = mgr.revise(reason="strengthen release checks", contract={"verification": replacement}, evidence=output)

    assert result["ok"]
    state = load_goal(sid)
    assert state.revisions[-1]["authority"] == "evidence"
    assert state.contract.verification == replacement
    assert "Security   audit passes." in mgr.next_continuation_prompt()
    assert "Smoke test passes." in mgr.next_continuation_prompt()


@pytest.mark.parametrize("evidence,code", [
    ("short", "evidence_too_short"),
    ("Unrecorded audit retirement evidence.", "evidence_not_recorded"),
])
def test_removal_validates_evidence_provenance_before_refusing_retirement(evidence, code):
    mgr = GoalManager("revision-evidence-provenance")
    mgr.set("Ship X", contract=GoalContract(verification="Security audit passes."))
    before = mgr.state.to_json()

    result = mgr.revise(reason="retire audit", contract={"verification": ""}, evidence=evidence)

    assert result["error_code"] == code
    assert load_goal(mgr.session_id).to_json() == before
    assert mgr.state.to_json() == before
    assert mgr.is_active()
