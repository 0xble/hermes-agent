"""Recorded capability evidence reaches the goal judge without claiming success.

Regression: a skill deployment goal repeatedly reported completion while the judge
asked for a skill_view result already recorded in the same transcript.
"""

import json

import pytest

from hermes_cli import goals
from tools.skills_tool import _skill_view_with_bump, reset_skill_view_dedup


@pytest.fixture
def skill_goal(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    skill = home / "skills" / "demo-capability"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo-capability\ndescription: A goal verification fixture.\n---\n"
        "# Installed capability\n", encoding="utf-8",
    )
    (skill / "references" / "guide.md").write_text("# Installed guide\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    goals._DB_CACHE.clear()
    reset_skill_view_dedup()
    sid = "capability-evidence"
    db = goals._get_session_db()
    assert db is not None
    db.ensure_session(sid, source="telegram")
    manager = goals.GoalManager(session_id=sid)
    manager.set("Install the capability", contract=goals.GoalContract(
        verification="The installed skill loads through skill_view.",
    ))
    yield skill, sid, db, manager
    goals._DB_CACHE.clear()
    reset_skill_view_dedup()


def _record(db, sid, name, args, output, call_id):
    db.append_message(sid, "assistant", "", tool_calls=[{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }])
    return db.append_message(sid, "tool", output, tool_name=name, tool_call_id=call_id)


@pytest.mark.parametrize("variant", ["main", "reference", "dedup", "missing", "missing-reference", "ambiguous"])
def test_skill_load_outcome_reaches_judge(skill_goal, monkeypatch, variant):
    skill, sid, db, manager = skill_goal
    args = {"name": skill.name}
    file_path = "SKILL.md"
    if variant == "reference":
        file_path = args["file_path"] = "references/guide.md"
    elif variant == "missing-reference":
        args["file_path"] = "references/missing.md"
    elif variant == "missing":
        args["name"] = "missing-capability"
    elif variant == "ambiguous":
        other = skill.parent / "demo-capability-other"
        other.mkdir()
        (other / "SKILL.md").write_text("---\nname: demo-capability-other\n---\n# Other\n", encoding="utf-8")
        args["name"] = "demo-cap"
    success = variant in {"main", "reference", "dedup"}
    output = _skill_view_with_bump(args, task_id=sid)
    loaded = json.loads(output)
    assert loaded["success"] is success
    first_id = _record(db, sid, "skill_view", args, output, "load")
    citation = ("Installed capability" if file_path == "SKILL.md" else "Installed guide") if success else args["name"]
    if variant == "dedup":
        # Real repeat stub omits the body; the earlier concrete load remains
        # citable outside the recent ledger and after compaction.
        stub = _skill_view_with_bump(args, task_id=sid)
        assert json.loads(stub)["content_returned"] is False
        db.archive_and_compact(sid, [{"role": "user", "content": "Compacted context"}])
        _record(db, sid, "skill_view", args, stub, "repeat")
        for index in range(goals._EVIDENCE_MAX_ENTRIES + 1):
            _record(db, sid, "read_file", {"path": f"other-{index}"}, "other result", f"noise-{index}")
    prompts = []

    def judge_transport(_call, _system, prompt, _timeout):
        prompts.append(prompt)
        return json.dumps({"verdict": "done" if success else "continue", "reason": "recorded load outcome"})

    monkeypatch.setattr(goals, "_call_goal_judge_llm", judge_transport)
    response = f"Installed and done. Evidence: `{citation}`."
    citations = goals.resolve_cited_evidence(sid, response, since=manager.state.created_at)
    decision = manager.evaluate_after_turn(response)

    assert str(first_id) in citations["evidence_ids"]
    assert "skill_view result #" in prompts[0]
    if success:
        assert citation in prompts[0]
        assert decision["status"] == "done" and not decision["should_continue"]
    else:
        assert '"success": false' in prompts[0]
        assert decision["status"] == "active" and decision["should_continue"]
        assert goals.resolve_cited_evidence(sid, "Evidence: `never-recorded-proof`")["unresolved"]
    persisted = goals.load_goal(sid)
    assert persisted is not None and persisted.status == decision["status"]


@pytest.mark.parametrize("name", ["skill_view", "skills_list", "skill_manage", "tool_search", "tool_describe"])
def test_capability_results_are_evidence_not_bookkeeping(skill_goal, name):
    _, sid, db, _ = skill_goal
    proof = "capability-proof-123456"
    row_id = _record(db, sid, name, {"name": "demo-capability"}, proof, "capability")

    assert goals.collect_goal_evidence(sid)[0]["tool"] == name
    citations = goals.resolve_cited_evidence(sid, f"Evidence: `{proof}`")
    assert str(row_id) in citations["evidence_ids"]
    assert goals._evidence_only_read_only_status(goals.collect_goal_evidence(sid)) is (name != "skill_manage")
