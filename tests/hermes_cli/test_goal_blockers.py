"""Blocker context survives the real judge/SQLite/control boundary."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli.goals import GoalManager, GoalState, load_goal


def test_blocker_roundtrip_resume_and_user_hold(monkeypatch):
    blocker = {
        "kind": "external_dependency",
        "detail": "Owner authorization is missing; all allowed local checks are complete.",
        "evidence": "Access check returned forbidden; no independent work remains.",
        "resume_when": "Owner grants staging access, then verify access before writing.",
    }
    reply = {"verdict": "blocked", "reason": blocker["detail"], "blocker": blocker}
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))])
    )
    manager = GoalManager("blocker-roundtrip")
    manager.set("Verify staging provider and runtime")
    decision = manager.evaluate_after_turn("All permitted checks complete; access is forbidden.")
    saved = load_goal("blocker-roundtrip")
    assert saved.status == "paused" and not saved.user_stopped
    assert saved.blocker == blocker
    assert not decision["should_continue"] and decision["verdict"] == "blocked"
    assert blocker["resume_when"] in decision["message"]
    assert "unachievable" not in decision["message"] and "override" not in decision["message"]
    restored = GoalManager("blocker-roundtrip")
    restored.resume()
    assert restored.state.blocker == blocker
    from hermes_cli.goal_display import format_goal_change
    notice = format_goal_change("resume", restored.state)
    assert blocker["resume_when"] in notice and "not permission" in notice
    assert blocker["resume_when"] in restored.next_continuation_prompt()
    assert "not permission" in restored.next_continuation_prompt()
    restored.pause()
    with pytest.raises(ValueError, match="User-stopped"):
        GoalManager("blocker-roundtrip").resume(user_requested=False)
    restored.resume()
    reply.update(verdict="continue", reason="Access granted; perform readback")
    resumed = restored.evaluate_after_turn("Owner granted access; readback remains.")
    assert resumed["should_continue"] and load_goal("blocker-roundtrip").blocker is None


def test_legacy_and_incomplete_blockers_are_not_impossibility(monkeypatch):
    legacy = GoalState.from_json('{"goal":"verify provider","status":"paused","last_verdict":"blocked"}')
    assert legacy.blocker is None
    manager = GoalManager("legacy-blocker")
    manager.set("Verify provider")
    reply = {"verdict": "blocked", "reason": "Owner must provide access"}
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))])
    )
    decision = manager.evaluate_after_turn("No authorized next step without access")
    assert decision["status"] == "paused"
    assert manager.state.blocker["kind"] == "unspecified"
    assert manager.state.blocker["detail"] == reply["reason"]
    manager.resume()
    reply["blocker"] = {"kind": "unachievable_as_stated", "detail": "Impossible", "resume_when": "Change scope"}
    manager.evaluate_after_turn("Impossible")
    assert manager.state.blocker["kind"] == "unspecified"


@pytest.mark.parametrize("kind", [[], {}, 7, None])
def test_malformed_kind_is_bounded_diagnostic_not_a_parser_failure(kind):
    from hermes_cli.goals_blockers import normalize_blocker
    blocker = normalize_blocker({"kind": kind, "detail": "x" * 1000, "resume_when": []})
    assert blocker["kind"] == "unspecified"
    assert len(blocker["detail"]) == 800
    assert blocker["resume_when"]


def test_kanban_handoffs_and_worker_report_same_resumption_context(monkeypatch):
    from hermes_cli.kanban import _goal_mode_handoff_rejection
    from hermes_cli.goals import run_kanban_goal_loop
    from tools.kanban_tools import _goal_gate, _Reject

    blocker = {
        "kind": "external_dependency", "detail": "Required access is denied.",
        "evidence": "The authorized endpoint returned 403; local work is complete.",
        "resume_when": "Owner restores access; verify the endpoint before publishing.",
    }
    response = json.dumps({"verdict": "blocked", "reason": blocker["detail"], "blocker": blocker})
    monkeypatch.setattr("agent.auxiliary_client.get_text_auxiliary_client", lambda *a, **kw: (object(), "judge"))
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=response))]))
    # A manual task has identity but no dispatched attempt; it cannot donate worker evidence.
    task = SimpleNamespace(id="t1", current_run_id=None, title="Publish",
                           body="Verify endpoint access", goal_mode=True)
    verdict, notice = _goal_mode_handoff_rejection(task, "403; all local work complete")
    assert verdict == "blocked" and blocker["resume_when"] in notice
    for tool in ("kanban_complete", "kanban_request_review"):
        with pytest.raises(_Reject) as rejected:
            _goal_gate(tool, task, "t1", "403; all local work complete")
        assert blocker["resume_when"] in str(rejected.value)
        assert "unachievable" not in str(rejected.value)
    blocks = []
    result = run_kanban_goal_loop(
        task_id="t1", goal_text="Publish", first_response="403; all local work complete",
        run_turn=lambda prompt: pytest.fail("must not spend another blocked turn"),
        task_status_fn=lambda: "running", block_fn=blocks.append,
    )
    assert result["outcome"] == "blocked" and result["blocker"] == blocker
    assert len(blocks) == 1 and blocker["resume_when"] in blocks[0]
