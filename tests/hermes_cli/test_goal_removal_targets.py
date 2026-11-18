"""Exact existing requirement names are data, not removal authority.

Synthetic principals reproduce unquoted negative names, noun wrappers and explanatory
scope suffixes through GoalManager and SessionDB, without private transcript data.
"""

import json
import os
from pathlib import Path

import pytest

import hermes_bootstrap  # same bootstrap as the application entry point
from hermes_cli import goals


@pytest.fixture
def removal_goal(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    for key in os.environ:
        if key.startswith("HERMES_SESSION_"):
            monkeypatch.delenv(key, raising=False)
    (tmp_path / "config.yaml").write_text("goals:\n  autonomy: true\n", encoding="utf-8")
    goals._DB_CACHE.clear()
    db = goals._get_session_db()
    assert db is not None
    sid = "synthetic-removal-principal"
    db.create_session(sid, "telegram", profile_name="default", chat_id=sid,
                      chat_type="private", session_key=f"agent:main:telegram:dm:{sid}")

    def create(target, field):
        manager = goals.GoalManager(sid)
        contract = dict(outcome="Ship the complete repair", verification="Native checks pass; Readback matches",
                        constraints="Preserve audit history; Parent controls activation",
                        boundaries="Only the fixture", stop_when="Wait for an external dependency")
        subgoals = ["Delivery confirmed", "History retained"]
        if field == "subgoals":
            subgoals.append(target)
        else:
            contract[field] += "; " + target
        manager.set("Deliver the repair", max_turns=17, contract=goals.GoalContract(**contract))
        for subgoal in subgoals:
            manager.add_subgoal(subgoal)
        return manager

    yield db, sid, create
    goals._DB_CACHE.clear()


@pytest.mark.parametrize("field,target,source,action", [
    (*case, action) for case in [
    ("constraints", "No customer data on personal systems.",
     "remove the constraint No customer data on personal systems"),
    ("constraints", "No customer data on personal systems.",
     "remove no customer data on personal systems rule, including anywhere that's mentioned including memory (invalidate)"),
    ("constraints", "No customer data on personal systems.",
     "REMOVE THE RULE no CUSTOMER data on personal SYSTEMS."),
    ("constraints", "No customer data on personal systems.",
     '[Replying to: "Keep the rule unless explicitly removed"]\n\nremove the constraint No customer data on personal systems'),
    ("constraints", "Retain diagnostic logs", "remove the constraint Retain diagnostic logs"),
    ("constraints", "Never export private files", "please remove the requirement Never export private files!"),
    ("constraints", "Do not export private files", "remove Do not export private files"),
    ("constraints", "Run only after checks pass", "remove the criterion Run only after checks pass"),
    ("constraints", "No customer data on personal systems.",
     'remove the rule "No customer data on personal systems".'),
    ("constraints", "Retain diagnostic logs", "remove Retain diagnostic logs rule, including memory"),
    ("constraints", "Retain diagnostic logs.", "remove the requirement Retain diagnostic logs."),
    ("verification", "Never lose diagnostic logs", "remove the criterion Never lose diagnostic logs"),
    ("subgoals", "No missing readbacks", "remove the requirement No missing readbacks"),
    ("subgoals", "Never restart; remove caches only if approved.",
     "remove the rule Never restart; remove caches only if approved."),
    ] for action in (["revise"] if case[0] == "subgoals" else ["revise", "replace"])
])
def test_explicit_removal_binds_only_the_exact_existing_requirement(removal_goal, action, field, target, source):
    db, sid, create = removal_goal
    manager = create(target, field)
    before = json.loads(db.get_meta("goal:" + sid))
    remaining = [item for item in goals.contract_items(field, before.get(field, before["contract"].get(field)))
                 if item != target]
    db.append_message(sid, "user", source)
    quote = source.split("\n\n")[-1]
    edit = {"subgoals": remaining} if field == "subgoals" else {"contract": {field: "; ".join(remaining)}}
    if action == "replace":
        edit["goal"] = before["goal"]
    result = getattr(manager, action)(reason="Synthetic principal removed this requirement", user_quote=quote, **edit)
    assert result["ok"], result
    after = json.loads(db.get_meta("goal:" + sid))
    revision = after.pop("revisions")[-1]
    assert revision["quote_session_id"] == sid and revision["quote_message"] == quote
    assert revision["user_quote"] == quote
    before.pop("revisions")
    if field == "subgoals":
        before[field] = remaining
    else:
        before["contract"][field] = "; ".join(remaining)
    if action == "replace":
        assert after["created_at"] > before["created_at"]
        before["created_at"] = after["created_at"]
    assert after.pop("mutation_id") != before.pop("mutation_id")
    assert after == before
    persisted = goals.load_goal(sid)
    assert persisted is not None
    assert persisted.to_json() == manager.state.to_json()


@pytest.mark.parametrize("scenario,source", [
    ("plain", "Do not remove the constraint No customer data on personal systems"),
    ("plain", "If needed, remove the constraint No customer data on personal systems"),
    ("plain", "remove No customer data on personal systems if checks pass"),
    ("plain", "remove No customer data on personal systems rule, including memory if checks pass"),
    ("plain", "remove No customer data on personal systems rule, including memory provided checks pass"),
    ("plain", "remove No customer data on personal systems rule, including memory subject to checks passing"),
    ("plain", "Example: remove No customer data on personal systems"),
    ("plain", '"remove No customer data on personal systems"'),
    ("plain", "`remove No customer data on personal systems`"),
    ("plain", "> remove No customer data on personal systems"),
    ("plain", "remove No customer data on personal systems?"),
    ("plain", "remove No customer data on personal systems. Actually keep it."),
    ("plain", 'remove No customer data on personal systems. "Actually keep it"'),
    ("plain", "remove No customer data on personal systems. `Actually keep it`"),
    ("plain", "remove the constraint customer data on personal systems"),
    ("plain", "remove No customer data on personal systems backups"),
    ("plain", "remove prefix No customer data on personal systems"),
    ("plain", "remove No customer data on personal systems or Parent controls activation"),
    ("plain", "remove No customer data on personal systems but preserve it"),
    ("plain", "remove No customer data on personal systems rule, including nothing"),
    ("plain", "remove No customer data on personal systems rule, including memory actually cancel that"),
    ("plain", "remove No customer data on personal systems rule, including example memory"),
    ("plain", "remove No customer data on personal systems rule, including memory (do not invalidate)"),
    ("plain", "remove No customer data on personal systems, including memory pending legal review"),
    ("plain", "remove No customer data on personal systems, including memory depending on legal review"),
    ("plain", "remove No customer data on personal systems, including memory subject-to legal approval"),
    ("plain", "remove No customer data on personal systems, including memory while we are testing"),
    ("plain", "remove No customer data on personal systems, including memory during the trial"),
    ("plain", "remove No customer data on personal systems, including memory for now"),
    ("plain", "remove No customer data on personal systems, including memory temporarily"),
    ("plain", "remove No customer data on personal systems, including memory effective tomorrow"),
    ("plain", "remove No customer data on personal systems, including backups"),
    ("plain", "remove Retain diagnostic logs"),
    ("extra_delta", "remove the constraint No customer data on personal systems"),
    ("overlapping", "remove No customer data on personal systems rule"),
    ("stale", "remove the constraint No customer data on personal systems"),
    ("relay", "remove the constraint No customer data on personal systems"),
    ("revoked_at_commit", 'remove "No customer data on personal systems"'),
    ("revoked_at_commit", "remove the constraint No customer data on personal systems"),
])
def test_unbound_or_non_authoritative_removal_preserves_persisted_bytes(removal_goal, monkeypatch, scenario, source):
    db, sid, create = removal_goal
    manager = create("No customer data on personal systems.", "constraints")
    if scenario == "overlapping":
        result = manager.revise(reason="Add a distinct overlapping fixture requirement", contract={
            "constraints": manager.state.contract.constraints + "; No customer data on personal systems rule"})
        assert result["ok"], result
    before = db.get_meta("goal:" + sid)
    quoted = ("[relay from=hermes:default/synthetic-other receipt=synthetic-receipt]\n" + source
              if scenario == "relay" else source)
    db.append_message(sid, "user", quoted)
    if scenario == "stale":
        db.append_message(sid, "user", "Keep the limit after all")
    if scenario == "revoked_at_commit":
        execute = db._execute_write

        def revoke(commit, *args, **kwargs):
            monkeypatch.setattr(db, "_execute_write", execute)
            db.append_message(sid, "user", "Keep the limit after all")
            return execute(commit, *args, **kwargs)

        monkeypatch.setattr(db, "_execute_write", revoke)
    constraints = "Preserve audit history; Parent controls activation"
    if scenario == "overlapping":
        constraints += "; No customer data on personal systems rule"
    if scenario == "extra_delta":
        constraints = "Parent controls activation"
    result = manager.revise(reason="Synthetic refusal fixture", contract={"constraints": constraints}, user_quote=source)
    assert not result["ok"], result
    assert result["error_code"] == ("state_changed" if scenario == "revoked_at_commit" else "constraint_quote_required")
    assert db.get_meta("goal:" + sid) == before
    assert manager.state.to_json() == before
