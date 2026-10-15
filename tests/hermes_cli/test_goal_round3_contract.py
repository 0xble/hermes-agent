"""Round-three goal contract invariants: current state is the only prompt authority."""

from __future__ import annotations

import json

import pytest

from hermes_cli import goals
from hermes_cli.goal_command import dispatch_goal_command
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


def _dispatch(mgr: GoalManager, text: str):
    return dispatch_goal_command(mgr, text, authorize_gate=lambda: None)


def _tool(db, sid: str, output: str) -> None:
    call_id = "evidence"
    db.append_message(sid, "assistant", "", tool_calls=[{
        "id": call_id,
        "type": "function",
        "function": {"name": "terminal", "arguments": json.dumps({"command": "release verify"})},
    }])
    db.append_message(sid, "tool", output, tool_name="terminal", tool_call_id=call_id)


def _capture_judge(monkeypatch):
    prompts = []

    def capture(_call, _system, prompt, _timeout):
        prompts.append(prompt)
        return '{"verdict":"continue","reason":"still working"}'

    monkeypatch.setattr(goals, "_call_goal_judge_llm", capture)
    return prompts


def _assert_prompt_has_only_current(prompt: str, *, current: str, old: str) -> None:
    assert current in prompt
    assert old not in prompt
    assert "Revision history" not in prompt
    assert "earlier constraints" not in prompt
    assert "earlier boundaries" not in prompt
    assert "earlier stop_when" not in prompt
    assert "earlier outcome" not in prompt
    assert "earlier verification" not in prompt
    assert "earlier goal" not in prompt
    assert "dropped criteria" not in prompt


def test_reintroduced_verification_cannot_be_reworded_without_command(hermes_home):
    mgr = GoalManager(session_id="round3-reintroduced")
    mgr.set("Ship X", contract=GoalContract(verification="Security audit passes."))
    assert _dispatch(mgr, "remove check 1").error is False

    assert mgr.revise(
        reason="the audit is required again",
        contract={"verification": "Security audit passes."},
    )["ok"]
    before_state = load_goal(mgr.session_id)
    assert before_state is not None
    before = before_state.to_json()
    result = mgr.revise(
        reason="use a smoke test instead",
        contract={"verification": "Smoke test passes."},
    )

    assert result["error_code"] == "destructive_change_requires_command"
    after_state = load_goal(mgr.session_id)
    assert after_state is not None
    assert after_state.to_json() == before
    assert mgr.is_active()


@pytest.mark.parametrize("field", ["constraints", "boundaries", "stop_when", "outcome", "verification", "subgoals"])
def test_case_change_is_destructive_not_additive(hermes_home, field):
    mgr = GoalManager(session_id="round3-case")
    original, changed = "Only deploy to tenant ACME.", "Only deploy to tenant acme."
    mgr.set("Ship X", contract=GoalContract(**{field: original}) if field != "subgoals" else None)
    if field == "subgoals":
        mgr.add_subgoal(original)
    result = mgr.revise(
        reason="normalize the tenant name",
        subgoals=[changed] if field == "subgoals" else None,
        contract={field: changed} if field != "subgoals" else None,
    )

    assert result["ok"]
    after_state = load_goal(mgr.session_id)
    assert after_state is not None
    if field == "subgoals":
        assert after_state.subgoals == [changed]
    else:
        assert getattr(after_state.contract, field) == changed
    assert mgr.is_active()


def test_whitespace_only_identity_changes_are_additive(hermes_home):
    mgr = GoalManager(session_id="round3-whitespace")
    mgr.set("Ship X", contract=GoalContract(constraints="Only  deploy to tenant ACME."))
    mgr.add_subgoal("Ship  CLI. Add tests.")
    assert mgr.revise(reason="normalize whitespace", contract={
        "constraints": "Only deploy to tenant ACME. No downtime.",
    }, subgoals=["Ship CLI.  Add tests.", "Document it"])["ok"]
    state = mgr.state
    assert state is not None
    assert goals.contract_items("subgoals", state.subgoals)[0] == "Ship CLI. Add tests."


def test_status_numbers_every_current_binding_item(hermes_home):
    mgr = GoalManager(session_id="round3-status")
    mgr.set(
        "Ship X",
        contract=GoalContract(
            constraints="No downtime.; Only deploy to tenant ACME.",
            verification="Security audit passes.; Smoke test passes.",
        ),
    )
    mgr.add_subgoal("Update the release notes")
    assert _dispatch(mgr, "remove check 1").error is False
    assert mgr.revise(
        reason="restore the required audit",
        contract={"verification": "Security audit passes.; Smoke test passes."},
    )["ok"]

    status = _dispatch(mgr, "status").output
    assert "- 1. No downtime." in status
    assert "- 2. Only deploy to tenant ACME." in status
    assert "- 1. Security audit passes." in status
    assert "- 2. Smoke test passes." in status
    assert "- 1. Update the release notes" in status
    result = mgr.revise(reason="replace the checks", contract={"verification": "Quick check passes."})
    assert result["error_code"] == "destructive_change_requires_command"
    assert _dispatch(mgr, "status").output == status


def test_evidence_verification_revision_prompt_uses_current_contract_only(hermes_home, monkeypatch):
    sid = "round3-evidence-prompt"
    db = goals._get_session_db()
    assert db is not None
    db.ensure_session(sid, source="telegram")
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship X", contract=GoalContract(verification="Old gate passes."))
    _tool(db, sid, "Signed release v2 is published and verified.")
    assert mgr.revise(
        reason="add the signed release check",
        contract={"verification": "Old gate passes.\nSigned release v2 exists."},
        evidence="Signed release v2 is published and verified.",
    )["ok"]

    continuation = mgr.next_continuation_prompt()
    assert continuation is not None
    _assert_prompt_has_only_current(continuation, current="Signed release v2 exists.", old="add the signed release check")
    assert "Old gate passes." in continuation
    prompts = _capture_judge(monkeypatch)
    assert mgr.evaluate_after_turn("working")["should_continue"]
    _assert_prompt_has_only_current(prompts[0], current="Signed release v2 exists.", old="add the signed release check")
    assert "Old gate passes." in prompts[0]


def test_remove_prompt_uses_current_contract_only(hermes_home, monkeypatch):
    mgr = GoalManager(session_id="round3-remove-prompt")
    mgr.set("Ship X", contract=GoalContract(constraints="No downtime.; No secrets."))
    assert _dispatch(mgr, "remove constraint 1").error is False

    continuation = mgr.next_continuation_prompt()
    assert continuation is not None
    _assert_prompt_has_only_current(continuation, current="No secrets.", old="No downtime.")
    prompts = _capture_judge(monkeypatch)
    assert mgr.evaluate_after_turn("working")["should_continue"]
    _assert_prompt_has_only_current(prompts[0], current="No secrets.", old="No downtime.")


def test_replace_prompt_uses_current_contract_only(hermes_home, monkeypatch):
    mgr = GoalManager(session_id="round3-replace-prompt")
    mgr.set("Old objective", contract=GoalContract(verification="Old gate passes."))
    assert not _dispatch(mgr, "replace New objective").error

    continuation = mgr.next_continuation_prompt()
    assert continuation is not None
    _assert_prompt_has_only_current(continuation, current="New objective", old="Old objective")
    prompts = _capture_judge(monkeypatch)
    assert mgr.evaluate_after_turn("working")["should_continue"]
    _assert_prompt_has_only_current(prompts[0], current="New objective", old="Old objective")
