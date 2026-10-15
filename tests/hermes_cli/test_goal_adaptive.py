"""Adaptive goals: cited evidence reaches the judge, disputes pause only without new evidence,
long responses keep their closing evidence, and goal revisions are versioned and authority-checked.

Regression sources (read-only replay of the live store, 2026-09-28): two disputed pauses where the
judge said the gate result / plugin test count was missing while the tool results sat 69 and 16
results before the completion claim, outside the last-8 evidence window; and goals whose user
descoping could only be appended as subgoals while the superseded criteria stayed binding.
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


def _db(session_id):
    db = goals._get_session_db()
    assert db is not None
    db.ensure_session(session_id, source="telegram")
    return db


def _tool(db, sid, name, args, output, call_id):
    db.append_message(sid, "assistant", "", tool_calls=[{
        "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}])
    db.append_message(sid, "tool", output, tool_name=name, tool_call_id=call_id)


def _capture(monkeypatch, replies):
    prompts, replies = [], iter(replies)

    def capture(_call, _system, prompt, _timeout):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(goals, "_call_goal_judge_llm", capture)
    return prompts


GATE = json.dumps({"output": "gate_exit=0 exact_sha=94d6cf5891ffe6f42ba720d4966a2b6a466f29d3", "exit_code": 0})
CLAIM = ("Done. Evidence: the gate `exact_sha=94d6cf5891ffe6f42ba720d4966a2b6a466f29d3` exited 0 "
         "and the suite printed `65 passed in 32.49s`.")


# ── cited evidence ─────────────────────────────────────────────────────


def test_extract_citations_keeps_backticked_identifiers_inside_details():
    response = (
        "Done.\n<details><summary>Evidence</summary>\n"
        "- `abc1234def` is the shipped commit.\n"
        "- `run_id=run_0123456789` passed.\n</details>"
    )

    citations = goals.extract_citations(response)

    assert "abc1234def" in citations
    assert "run_id=run_0123456789" in citations


def test_details_evidence_outside_recent_window_reaches_judge(hermes_home, monkeypatch):
    sid = "cite-details"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship it", contract=GoalContract(verification="commit shipped; run passed"))
    proof = "shipped abc1234def; run_id=run_0123456789 passed"
    _tool(db, sid, "terminal", {"command": "verify-release"}, proof, "release")
    for i in range(goals._EVIDENCE_MAX_ENTRIES + 1):
        _tool(db, sid, "read_file", {"path": f"f{i}"}, f"content {i}", f"r{i}")
    response = (
        "Done.\n<details><summary>Evidence</summary>\n"
        "- `abc1234def` is the shipped commit.\n"
        "- `run_id=run_0123456789` passed.\n</details>"
    )
    prompts = _capture(monkeypatch, ['{"verdict":"done","reason":"cited proof located"}'])

    assert mgr.state is not None
    citations = goals.resolve_cited_evidence(sid, response, since=mgr.state.created_at)
    decision = mgr.evaluate_after_turn(response)

    assert citations["unresolved"] == []
    assert {"abc1234def", "run_id=run_0123456789"} <= {c["needle"] for c in citations["cited"]}
    assert any(proof in c["excerpt"] for c in citations["cited"])
    assert len(citations["evidence_ids"]) == 1
    assert {str(c["message_id"]) for c in citations["cited"]} == set(citations["evidence_ids"])
    assert decision["verdict"] == "done"
    assert response in prompts[0]
    cited = prompts[0].split("Evidence the response cites")[1]
    assert proof in cited
    assert proof not in prompts[0].split("Tool results recorded")[1]


def test_cited_evidence_outside_the_recent_window_reaches_the_judge(hermes_home, monkeypatch):
    sid = "cite-old"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship it", contract=GoalContract(verification="gate passes on the exact head; tests pass"))
    _tool(db, sid, "terminal", {"command": "./bin/ci gate"}, GATE, "gate")
    _tool(db, sid, "terminal", {"command": "pytest"}, '{"output": "65 passed in 32.49s"}', "tests")
    for i in range(goals._EVIDENCE_MAX_ENTRIES + 20):
        _tool(db, sid, "read_file", {"path": f"f{i}"}, f"content {i}", f"r{i}")
    prompts = _capture(monkeypatch, ['{"verdict":"done","reason":"cited evidence located"}'])

    decision = mgr.evaluate_after_turn(CLAIM)

    assert decision["verdict"] == "done"
    cited = prompts[0].split("Evidence the response cites")[1]
    assert "gate_exit=0 exact_sha=94d6cf5891ffe6f42ba720d4966a2b6a466f29d3" in cited
    assert "65 passed in 32.49s" in cited
    # Outside the recency window: the ledger alone would not have shown them.
    assert "exact_sha=94d6cf5" not in prompts[0].split("Tool results recorded")[1]


@pytest.mark.parametrize("ellipsis", ["…", "..."])
def test_truncated_identifier_matches_real_prefix_and_shows_full_output(hermes_home, ellipsis):
    sid = "cite-truncated"
    db = _db(sid)
    _tool(db, sid, "terminal", {"command": "deploy"},
          "deployed dpl_EtykL1234567890abcdef successfully", "deploy")

    needle = f"dpl_EtykL{ellipsis}"
    result = goals.resolve_cited_evidence(sid, f"Deployment `{needle}`.")

    assert result["unresolved"] == []
    assert result["cited"][0]["needle"] == needle
    assert "dpl_EtykL1234567890abcdef" in result["cited"][0]["excerpt"]


@pytest.mark.parametrize("host,short", [("github.com/0xble/agents", False),
                                         ("git.example.org/acme/repo", True)])
def test_commit_url_matches_hash_in_tool_result_not_an_invented_url(hermes_home, host, short):
    sid = "cite-commit-url"
    db = _db(sid)
    sha = "9351a5317f1e063690e0f3698677f3a832e1251a"
    _tool(db, sid, "terminal", {"command": "git rev-parse HEAD"}, f"{sha}\n", "sha")
    cited_hash = sha[:7] if short else sha
    url = f"https://{host}/commit/{cited_hash}"

    result = goals.resolve_cited_evidence(sid, f"Committed `{url}`.")

    assert result["unresolved"] == []
    cited = next(c for c in result["cited"] if c["needle"] == url)
    assert any(sha in c["excerpt"] for c in result["cited"])
    assert "matched by commit hash" in cited["excerpt"]
    assert "URL not verified" in goals._render_cited_block(result)


def test_fabricated_truncations_and_commit_urls_stay_unresolved(hermes_home):
    sid = "cite-fabricated-shapes"
    db = _db(sid)
    _tool(db, sid, "terminal", {"command": "deploy"}, "dpl_EtykL1234567890abcdef dpl_123extra", "deploy")
    _tool(db, sid, "terminal", {"command": "git rev-parse HEAD"},
          "9351a5317f1e063690e0f3698677f3a832e1251a", "sha")
    missing_id = "dpl_NoSuch123…"
    short_id = "dpl_123…"
    missing_url = "https://github.com/0xble/agents/commit/deadbeefcafe1234567890123456789012345678"
    result = goals.resolve_cited_evidence(
        sid, f"Evidence: `{missing_id}`, `{short_id}`, and `{missing_url}`.")

    assert missing_id in result["unresolved"]
    assert short_id in result["unresolved"]
    assert missing_url in result["unresolved"]
    assert not any(c["needle"] in (missing_id, short_id, missing_url) for c in result["cited"])


def test_shortened_shapes_do_not_launder_bookkeeping_or_typed_user_text(hermes_home):
    sid = "cite-shape-provenance"
    db = _db(sid)
    sha = "9351a5317f1e063690e0f3698677f3a832e1251a"
    _tool(db, sid, "memory", {"action": "add"}, f"dpl_EtykL1234567890 {sha}", "mem")
    db.append_message(sid, "user", f"[ASYNC DELEGATION BATCH COMPLETE] dpl_EtykL1234567890 {sha}")
    url = f"https://github.com/0xble/agents/commit/{sha}"

    result = goals.resolve_cited_evidence(sid, f"`dpl_EtykL…` `{url}`")

    assert "dpl_EtykL…" in result["unresolved"] and url in result["unresolved"]
    assert not any(c["needle"] in ("dpl_EtykL…", url) for c in result["cited"])


def test_fabricated_citations_are_listed_as_unverified(hermes_home, monkeypatch):
    sid = "cite-fake"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship it")
    _tool(db, sid, "terminal", {"command": "./bin/ci gate"}, GATE, "gate")
    prompts = _capture(monkeypatch, ['{"verdict":"continue","disputed":true,"reason":"unverified"}'])

    mgr.evaluate_after_turn("Done: `exact_sha=deadbeefcafe0000111122223333444455556666` and `999 passed in 0.01s`.")

    assert "NOT found in any recorded tool result" in prompts[0]
    unresolved = prompts[0].split("NOT found in any recorded tool result")[1]
    assert "deadbeefcafe0000111122223333444455556666" in unresolved and "999 passed" in unresolved
    assert "Evidence the response cites" not in prompts[0]


def test_agent_prose_and_bookkeeping_tools_never_count_as_cited_evidence(hermes_home):
    sid = "cite-prose"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship it")
    db.append_message(sid, "assistant", "I ran it: `release_id=rel_abcdef123456`")
    _tool(db, sid, "memory", {"action": "add"}, "noted release_id=rel_abcdef123456", "mem")
    _tool(db, sid, "goal_set", {"action": "status"}, "release_id=rel_abcdef123456", "gs")

    result = goals.resolve_cited_evidence(sid, "Evidence: `release_id=rel_abcdef123456`", since=mgr.state.created_at)

    assert result["cited"] == []
    assert "release_id=rel_abcdef123456" in result["unresolved"]


def test_cited_command_resolves_to_its_result_and_notices_count(hermes_home):
    sid = "cite-command"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship it")
    _tool(db, sid, "terminal", {"command": "agentkit check-live --mode enforce"},
          '{"output": "passed=True findings=0"}', "cl")
    db.append_message(sid, "user", "[ASYNC DELEGATION BATCH COMPLETE — deleg_1]\n"
                      '```json\n{"head_sha": "249640ec2cce05aa9e742e0543fb4535239ae91b", "verdict": "approve"}\n```',
                      display_kind="internal_notification")

    result = goals.resolve_cited_evidence(
        sid, 'Ran `check-live --mode enforce`; review `"verdict": "approve"` on '
             '`249640ec2cce05aa9e742e0543fb4535239ae91b`.', since=mgr.state.created_at)

    by_needle = {c["needle"]: c for c in result["cited"]}
    assert "passed=True findings=0" in by_needle["check-live --mode enforce"]["excerpt"]
    assert by_needle['"verdict": "approve"']["tool"].startswith("delegation result")
    assert result["unresolved"] == []


def test_ordinary_user_text_is_not_citable_evidence(hermes_home):
    sid = "cite-user"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship it")
    db.append_message(sid, "user", "please make sure `build_id=bld_12345678` is live")

    result = goals.resolve_cited_evidence(sid, "Live: `build_id=bld_12345678`", since=mgr.state.created_at)

    assert result["cited"] == [] and "build_id=bld_12345678" in result["unresolved"]


def test_cited_excerpts_are_redacted(hermes_home):
    sid = "cite-redact"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship it")
    secret = "ghp_" + "b" * 36
    _tool(db, sid, "terminal", {"command": "deploy"}, f"deploy_id=dep_0123456789 token={secret}", "d")

    result = goals.resolve_cited_evidence(sid, "Deployed `deploy_id=dep_0123456789`.", since=mgr.state.created_at)

    assert result["cited"] and secret not in result["cited"][0]["excerpt"]


def test_extraction_prefers_the_closing_evidence_and_skips_bare_words():
    body = " ".join(f"`item_{i:04d}_x`" for i in range(60))
    text = f"Summary {body}\n\nEvidence: `exact_sha=abcdef1234567` and 12 passed. `independent`"
    needles = goals.extract_citations(text)
    assert needles[0] == "12 passed" or "exact_sha=abcdef1234567" in needles[:3]
    assert "independent" not in needles
    assert len(needles) <= goals._CITATION_MAX_NEEDLES


def test_prompt_without_citations_is_unchanged(monkeypatch):
    prompts = _capture(monkeypatch, ['{"verdict":"continue","reason":"r"}'] * 2)
    goals.judge_goal("Finish the work", "did a thing", timeout=1)
    goals.judge_goal("Finish the work", "did a thing", citations={"cited": [], "unresolved": []}, timeout=1)
    assert prompts[0] == prompts[1]
    assert "Evidence the response cites" not in prompts[0] and "Revision history" not in prompts[0]


# ── response window ───────────────────────────────────────────────────


def test_judge_sees_the_closing_evidence_of_a_long_response(monkeypatch):
    prompts = _capture(monkeypatch, ['{"verdict":"continue","reason":"r"}'])
    response = "intro " + "x" * 12000 + " EVIDENCE: gate exit 0"
    goals.judge_goal("Finish", response, timeout=1)
    assert "EVIDENCE: gate exit 0" in prompts[0]
    assert "chars omitted" in prompts[0]
    assert response not in prompts[0]


# ── dispute stall breaker ─────────────────────────────────────────────

DISPUTED = ("continue", "tests missing", False, {"disputed": True}, False)


# ── additive-only revisions and explicit destructive commands ────────────


def _dispatch(mgr, text):
    from hermes_cli.goal_command import dispatch_goal_command
    return dispatch_goal_command(mgr, text, authorize_gate=lambda: None)


def test_agent_verification_replacement_requires_evidence(hermes_home):
    mgr = GoalManager(session_id="rev-verify")
    mgr.set("Ship X", contract=GoalContract(verification="12-item checklist"))
    before_state = load_goal("rev-verify")
    assert before_state is not None
    before = before_state.to_json()
    result = mgr.revise(reason="checklist moved to plan file", contract={"verification": "outcome holds live"})
    assert result["error_code"] == "destructive_change_requires_command"
    after_state = load_goal("rev-verify")
    assert after_state is not None
    assert after_state.to_json() == before
    assert mgr.is_active()


def test_agent_additions_are_allowed_without_user_prose(hermes_home):
    mgr = GoalManager(session_id="rev-add")
    mgr.set("Ship X")
    result = mgr.revise(reason="learned more acceptance criteria", contract={
        "constraints": "No secrets.", "verification": "Run the smoke test."},
        subgoals=["also migrate Willow"])
    assert result["ok"]
    state = load_goal("rev-add")
    assert state.contract.constraints == "No secrets."
    assert state.subgoals == ["also migrate Willow"]


@pytest.mark.parametrize("change", [
    {"goal": "Ship a smaller X"},
    {"contract": {"constraints": ""}},
    {"subgoals": []},
    {"contract": {"constraints": "Ship only after review."}},
])
def test_agent_destructive_revision_is_refused_unchanged_and_running(hermes_home, change):
    mgr = GoalManager(session_id="rev-refuse")
    mgr.set("Ship X", contract=GoalContract(constraints="No downtime."))
    mgr.add_subgoal("also migrate Willow")
    before = load_goal("rev-refuse").to_json()
    result = mgr.revise(reason="model saw a scope change", **change)
    assert result["error_code"] == "destructive_change_requires_command"
    assert "/goal replace <text>" in result["error"]
    after = load_goal("rev-refuse")
    assert after.to_json() == before
    assert after.status == "active"


def test_agent_additions_ignore_legacy_user_quote_without_authority(hermes_home):
    mgr = GoalManager(session_id="rev-legacy-quote")
    mgr.set("Ship X")
    result = mgr.revise(
        reason="learned a check", contract={"verification": "Smoke test passes."},
        user_quote="legacy plugin argument is ignored",
    )
    assert result["ok"]
    state = load_goal("rev-legacy-quote")
    assert state.contract.verification == "Smoke test passes."
    assert "user_quote" not in state.revisions[-1]


@pytest.mark.parametrize("message", [
    "Please do not remove the quality gates and turn budget.",
    'Remove the feature gate, and note that README says "No downtime."',
    "Replace the goal with Ship Y. Please do not remove the quality gates.",
    "Please keep going; remove old restriction.",
])
def test_free_form_bypass_text_cannot_authorize_agent_revision(hermes_home, message):
    mgr = GoalManager(session_id="rev-bypass")
    mgr.set("Ship X", contract=GoalContract(constraints="No downtime."))
    before = load_goal("rev-bypass").to_json()
    # The model path receives structured fields, never a prose authorization channel.
    result = mgr.revise(reason=message, contract={"constraints": ""})
    assert result["error_code"] == "destructive_change_requires_command"
    assert load_goal("rev-bypass").to_json() == before
    assert mgr.is_active()


@pytest.mark.parametrize("message, action, item, value", [
    ("Replace the goal with Ship Y and verify the docs.", "replace", "", "Ship Y and verify the docs"),
    ("Replace the goal with Ship Y and verify the docs.", "goal", "", "Ship Y and verify the docs"),
    ("Change the objective to Fix README.md.", "replace", "", "Fix README.md"),
    ("Please replace the goal with:\nSHIP the docs!", "replace", "", "ship the docs."),
    ('Replace the goal with “Ship Y”.', "replace", "", "'Ship Y'"),
    ('Remove the constraint “production”.', "constraints", "production", ""),
    ("Remove 'Never publish secrets'.", "constraints", "Never publish secrets.", ""),
    ("Remove the downtime constraint.", "constraints", "No downtime. Never publish secrets.", "Never publish secrets."),
    ("Drop the staging criterion.", "subgoals", "Deploy the staging build before release", ""),
    pytest.param("Change constraints to allow public sharing", "constraints", "Never publish secrets", "Never publish secrets. Allow public sharing", id="r6-additive-retain"),
    pytest.param("Change verification to smoke tests pass", "verification", "Security audit passes", "Security audit passes. Smoke tests pass", id="r6-verification-additive-retain"),
    pytest.param("Change outcome to rollout is complete", "outcome", "Security audit passes", "Security audit passes. Rollout is complete", id="r6-outcome-additive-retain"),
    pytest.param("Change boundaries to docs repository", "boundaries", "Application repository", "Application repository. Docs repository", id="r6-boundaries-additive-retain"),
    pytest.param("Change stop condition to rollout is complete", "stop_when", "Security audit fails", "Security audit fails. Rollout is complete", id="r6-stop-condition-additive-retain"),
    pytest.param("Change subgoals to smoke tests pass", "subgoals", "Security audit passes", "Security audit passes. Smoke tests pass", id="r6-subgoal-additive-retain"),
    pytest.param("Remove 'No downtime'.", "constraints", "No downtime. Never publish secrets", "Never publish secrets", id="r6-remove-one-keep-others"),
    pytest.param("Remove 'No downtime'. Change constraints to allow public sharing", "constraints", "No downtime. Never publish secrets", "Never publish secrets. Allow public sharing", id="r6-remove-add-retain"),
    pytest.param("Change constraints to never expose secrets. Remove 'Never publish secrets'.", "constraints", "Never publish secrets", "Never expose secrets", id="r6-authorized-reword"),
    pytest.param("Drop 'Smoke tests pass'.", "subgoals", "Security audit passes. Smoke tests pass", "Security audit passes", id="r6-subgoal-remove-one-keep-others"),
    pytest.param("Change constraints to never expose secrets", "constraints", "NEVER EXPOSE SECRETS!", "Never expose secrets.", id="r6-normalized-retained"),
])
@pytest.mark.parametrize("compacted", [False, True], ids=["active", "carried-through-compaction"])
def test_exact_authority_remains_actionable_from_active_latest_input(
        hermes_home, message, action, item, value, compacted):
    sid = "exact-positive"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    field = action if action in goals._CONTRACT_FIELDS else "constraints"
    mgr.set("Ship original", max_turns=8, contract=GoalContract(**{field: item}))
    mgr.add_gate("printf gate")
    if action == "subgoals":
        mgr.add_subgoal(item)
    db.append_message(sid, "user", message)
    if compacted:
        db.archive_and_compact(sid, [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary", "compressed_summary": True},
            {"role": "user", "content": message},
        ], tail_count=1)
    if action == "replace":
        result = mgr.replace(reason="exact request", goal=value, user_quote=message)
    else:
        change = ({"goal": value} if action == "goal" else {"subgoals": [value] if value else []} if action == "subgoals"
                  else {"contract": {field: value}})
        result = mgr.revise(reason="exact request", user_quote=message, **change)
    # Agent/model calls cannot use recorded prose as destructive authority. Those edits now belong
    # to the explicit /goal replace/remove command path. Additive revisions remain valid here.
    destructive = action in {"replace", "goal", "subgoals"} or (
        item and not goals._normalize_authority_value(item) in {
            goals._normalize_authority_value(candidate)
            for candidate in goals.contract_items(field, value)
        }
    )
    if destructive:
        assert result["error_code"] == "destructive_change_requires_command"
        return
    assert result["ok"]
    persisted = load_goal(sid)
    assert persisted is not None
    assert persisted.goal == (value if action in {"replace", "goal"} else "Ship original")
    if action in goals._CONTRACT_FIELDS:
        assert getattr(persisted.contract, field) == value
    if action == "subgoals":
        assert persisted.subgoals == ([value] if value else [])
    assert persisted.status == "active" and persisted.max_turns == 8
    assert [gate.command for gate in persisted.gates] == ["printf gate"]
    assert persisted.waiting_until == 0.0
    assert persisted.waiting_on_pid is None and persisted.waiting_on_session is None


def test_mask_first_blocks_multi_sentence_quoted_and_fenced_replacements(hermes_home):
    for suffix in ("quoted", "fenced"):
        sid = f"replace-mask-first-{suffix}"
        db = _db(sid)
        mgr = GoalManager(session_id=sid)
        mgr.set("Ship X", contract=GoalContract(constraints="Never publish secrets"))
        message = (
            'The README says "Example. Replace the goal with Ship Y."'
            if suffix == "quoted"
            else "The README says ```Example. Replace the goal with Ship Y.```"
        )
        db.append_message(sid, "user", message)
        result = mgr.replace(reason="README mention", goal="Ship Y", user_quote=message)
        assert result["error_code"] == "destructive_change_requires_command"
        persisted = load_goal(sid)
        assert persisted is not None
        assert persisted.goal == "Ship X"
        assert persisted.contract.constraints == "Never publish secrets"
        assert persisted.status == "active" and persisted.waiting_until == 0.0


def test_removal_requires_coverage_not_one_overlapping_token(hermes_home):
    sid = "rev-coverage-production"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship X", contract=GoalContract(constraints="Never deploy to production"))
    db.append_message(sid, "user", "Remove the production rate limit from checkout.")

    result = mgr.revise(
        reason="checkout changed", contract={"constraints": ""},
        user_quote="Remove the production rate limit from checkout.",
    )

    assert result["error_code"] == "destructive_change_requires_command"
    persisted = load_goal(sid)
    assert persisted is not None
    assert persisted.contract.constraints == "Never deploy to production"
    assert persisted.status == "active" and persisted.waiting_until == 0.0


def test_removal_is_authorized_per_requirement_not_per_contract_field(hermes_home):
    sid = "rev-per-item"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship X", contract=GoalContract(constraints="No downtime. Never publish secrets."))
    db.append_message(sid, "user", "Remove the downtime constraint.")

    result = mgr.revise(
        reason="downtime is no longer relevant",
        contract={"constraints": "Never publish secrets."},
        user_quote="Remove the downtime constraint.",
    )

    assert result["error_code"] == "destructive_change_requires_command"
    persisted = load_goal(sid)
    assert persisted is not None
    assert persisted.contract.constraints == "No downtime. Never publish secrets."


def test_unrelated_directives_never_remove_a_requirement_by_generic_overlap(hermes_home):
    cases = [
        ("Never deploy to production", "Remove the production rate limit from checkout.", "constraints"),
        ("No downtime. Never publish secrets.", "Remove the downtime monitor from checkout.", "constraints"),
        ("Verify the staging rollback path", "Drop the staging schedule.", "subgoals"),
        ("Run the README.md check", "Delete the README example.", "verification"),
    ]
    for index, (item, directive, field) in enumerate(cases):
        sid = f"rev-property-negative-{index}"
        db = _db(sid)
        mgr = GoalManager(session_id=sid)
        contract = GoalContract(**{field: item}) if field != "subgoals" else GoalContract()
        mgr.set("Ship X", contract=contract)
        if field == "subgoals":
            mgr.add_subgoal(item)
        db.append_message(sid, "user", directive)
        change = {"subgoals": []} if field == "subgoals" else {"contract": {field: ""}}
        result = mgr.revise(reason="unrelated work", user_quote=directive, **change)
        assert result["error_code"] == "destructive_change_requires_command"
        assert mgr.state.status == "active" and mgr.state.waiting_until == 0.0
        if field == "subgoals":
            assert mgr.state.subgoals == [item]
        else:
            assert getattr(mgr.state.contract, field) == item


def test_destructive_revision_positive_directives_still_apply(hermes_home):
    db = _db("rev-d1-positive")
    mgr = GoalManager(session_id="rev-d1-positive")
    mgr.set("Ship X", contract=GoalContract(constraints="Never publish secrets"))
    mgr.add_subgoal("Deploy the staging build before release")

    db.append_message("rev-d1-positive", "user", "Remove the constraint 'Never publish secrets'.")
    removed = mgr.revise(reason="user removed the restriction", contract={"constraints": ""},
                         user_quote="Remove the constraint 'Never publish secrets'.")
    assert removed["error_code"] == "destructive_change_requires_command"

    db.append_message("rev-d1-positive", "user", "Drop the staging criterion.")
    dropped = mgr.revise(reason="user dropped staging", subgoals=[],
                         user_quote="Drop the staging criterion.")
    assert dropped["error_code"] == "destructive_change_requires_command"
    assert mgr.state.status == "active"
    assert mgr.state.waiting_until == 0.0


def test_combined_request_succeeds_as_two_destructive_revision_calls(hermes_home):
    sid = "rev-combined-two-calls"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship X", contract=GoalContract(constraints="Never publish secrets"))
    message = "Change the goal to Ship Y. Remove the constraint 'Never publish secrets'."
    db.append_message(sid, "user", message)

    objective = mgr.revise(reason="user changed objective", goal="Ship Y", user_quote=message)
    removal = mgr.revise(reason="user removed restriction", contract={"constraints": ""}, user_quote=message)

    assert objective["error_code"] == "destructive_change_requires_command"
    assert removal["error_code"] == "destructive_change_requires_command"
    assert mgr.state.goal == "Ship X"
    assert mgr.state.contract.constraints == "Never publish secrets"
    assert mgr.state.status == "active" and mgr.state.waiting_until == 0.0


def test_replacement_requires_an_explicit_unquoted_non_question_directive(hermes_home):
    db = _db("replace-d1-direct")
    mgr = GoalManager(session_id="replace-d1-direct")
    mgr.set("Ship X")

    cases = [
        "What happens if I replace the goal with Ship Y?",
        'The README says "Replace the goal with Ship Y."',
        "The README says 'Replace the goal with Ship Y.'",
        "If we replace the goal with Ship Y, tests may pass.",
        "```Replace the goal with Ship Y```",
    ]
    for message in cases:
        db.append_message("replace-d1-direct", "user", message)
        result = mgr.replace(reason="probe", goal="Ship Y", user_quote=message)
        assert result["error_code"] == "destructive_change_requires_command"
        assert mgr.state.goal == "Ship X"
        assert mgr.state.status == "active"
        assert mgr.state.waiting_until == 0.0

    direct = "Replace the goal with Ship Y."
    db.append_message("replace-d1-direct", "user", direct)
    result = mgr.replace(reason="explicit user direction", goal="Ship Y", user_quote=direct)
    assert result["error_code"] == "destructive_change_requires_command"
    assert mgr.state.goal == "Ship X"


def test_every_superseded_requirement_stays_visible_in_full(hermes_home):
    """An unauthorized weakening must not scroll out of the judge's view behind later revisions."""
    mgr = GoalManager(session_id="rev-window")
    original = "Run the security audit and every integration test. " + "x" * 600 + " END-OF-REQUIREMENT"
    mgr.set("Ship X", contract=GoalContract(verification=original))
    mgr.revise(reason="add context", contract={"verification": original + " Additional proof."})
    for i in range(6):
        mgr.revise(reason=f"add context {i}", contract={"outcome": f"X live v{i}"})
    assert original in mgr.next_continuation_prompt()


def test_a_real_quote_is_shown_with_its_full_message_for_the_judge_to_weigh(hermes_home, monkeypatch):
    """Substring presence proves the user said it, not that they authorized this change."""
    db = _db("rev-context")
    mgr = GoalManager(session_id="rev-context")
    mgr.set("Ship X", contract=GoalContract(constraints="Never publish secrets"))
    db.append_message("rev-context", "user", "Please remove the Never publish secrets constraint")
    result = mgr.revise(reason="loosen", contract={"constraints": ""},
                        user_quote="remove the Never publish secrets constraint")
    assert result["error_code"] == "destructive_change_requires_command"
    prompts = _capture(monkeypatch, ['{"verdict":"continue","reason":"r"}'])
    mgr.evaluate_after_turn("working")
    assert "Revision history" not in prompts[0]


def test_a_quoted_revise_is_not_a_scope_resetting_replacement(hermes_home, monkeypatch):
    """Only a user-authority goal replacement resets scope; a revise that cites the user does not."""
    mgr = GoalManager(session_id="rev-quoted-not-replace")
    mgr.set("Ship X", contract=GoalContract(constraints="Never publish secrets"))
    result = mgr.revise(reason="loosen", contract={"constraints": ""}, user_quote="Please keep going")
    assert result["error_code"] == "destructive_change_requires_command"
    prompts = _capture(monkeypatch, ['{"verdict":"continue","reason":"r"}'])
    mgr.evaluate_after_turn("working")
    assert "Revision history" not in prompts[0]


def test_optional_quote_must_meet_the_minimum_length(hermes_home):
    mgr = GoalManager(session_id="rev-short")
    mgr.set("Ship X")
    result = mgr.revise(reason="r", contract={"verification": "v2"}, user_quote="ok")
    assert result["ok"]


def test_citations_far_apart_in_one_result_each_get_an_excerpt(hermes_home):
    sid = "cite-far"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship it")
    body = "proof_start=aaaa1111 " + "filler " * 200 + " proof_end=bbbb2222"
    _tool(db, sid, "terminal", {"command": "report"}, body, "rep")
    result = goals.resolve_cited_evidence(sid, "Evidence: `proof_start=aaaa1111` and `proof_end=bbbb2222`.",
                                          since=mgr.state.created_at)
    block = goals._render_cited_block(result)
    # Each located citation's own text reaches the judge, though ~1,400 chars separate them.
    assert "proof_start=aaaa1111" in block.split("→", 1)[1] and "proof_end=bbbb2222" in block
    excerpted = [c["excerpt"] for c in result["cited"] if not c["excerpt"].startswith("(inside")]
    assert any("proof_start=aaaa1111" in e for e in excerpted)
    assert any("proof_end=bbbb2222" in e for e in excerpted)


def test_excluded_bookkeeping_matches_do_not_crowd_out_real_evidence(hermes_home):
    """Recent goal_set calls quoting an identifier must not hide the older result that proves it."""
    sid = "cite-crowd"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship it")
    _tool(db, sid, "terminal", {"command": "build"}, "build_id=bld_12345678 status=passed", "real")
    for i in range(6):
        _tool(db, sid, "goal_set", {"action": "subgoal", "text": f"check build_id=bld_12345678 #{i}"},
              f"noted build_id=bld_12345678 #{i}", f"bk{i}")
    result = goals.resolve_cited_evidence(sid, "Build `build_id=bld_12345678` passed.", since=mgr.state.created_at)
    assert not result["unresolved"]
    assert result["cited"] and {c["tool"] for c in result["cited"]} == {"terminal"}


@pytest.mark.parametrize("replacement", ["", "Smoke tests pass"])
def test_agent_proposed_verification_cannot_discard_binding_items(hermes_home, replacement):
    sid = "agent-proposed-binding-items"
    _db(sid)
    mgr = GoalManager(session_id=sid)
    previous = "Security audit passes. Production rollback is verified"
    mgr.set("Ship X", contract=GoalContract(verification=previous, constraints="Never publish secrets"))
    result = mgr.revise(reason="agent proposed a different check", contract={"verification": replacement})
    assert result["error_code"] == "destructive_change_requires_command"
    persisted = GoalManager(session_id=sid)
    assert persisted.state is not None
    assert not persisted.state.revisions or persisted.state.revisions[-1].get("actor") != "agent"
    assert persisted.state.contract.constraints == "Never publish secrets"
    continuation = persisted.next_continuation_prompt()
    assert continuation is not None
    assert previous in continuation
    assert previous in continuation


@pytest.mark.parametrize("change", [
    {"contract": {"constraints": "NEVER PUBLISH SECRETS!"}},
    {"contract": {"constraints": "Allow public sharing"}},
    {"goal": "Ship Y"},
])
def test_recorded_evidence_cannot_extend_verification_scope(hermes_home, change):
    sid = "evidence-verification-items-only"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship X", contract=GoalContract(
        verification="Security audit passes. Retired component is healthy",
        constraints="Never publish secrets"))
    recorded = "The approved plan removed the retired component entirely"
    _tool(db, sid, "read_file", {"path": "plan.md"}, recorded, "plan")
    assert mgr.state is not None
    before = mgr.state.to_json()
    result = mgr.revise(reason="retired component is obsolete", evidence=recorded,
                        **{**change, "contract": {"verification": "Security audit passes", **change.get("contract", {})}})
    assert not result["ok"]
    assert result["error_code"] in {"evidence_not_authorized", "destructive_revision_conflict", "destructive_change_requires_command"}
    unchanged = load_goal(sid)
    assert unchanged is not None
    assert mgr.state.to_json() == before == unchanged.to_json()

    allowed = mgr.revise(reason="retired component is obsolete", evidence=recorded,
                         contract={"verification": "Security audit passes"})
    assert allowed["error_code"] == "evidence_cannot_retire_verification"


def test_revision_keeps_the_complete_source_message(hermes_home, monkeypatch):
    """Context that negates the quoted words must reach the judge with them."""
    db = _db("rev-negated")
    mgr = GoalManager(session_id="rev-negated")
    mgr.set("Ship X", contract=GoalContract(verification="security audit passes"))
    message = "Do not drop the security audit requirement; tests pass."
    db.append_message("rev-negated", "user", message)
    result = mgr.revise(reason="r", contract={"verification": "tests pass"},
                        user_quote="Drop the security audit requirement")
    assert result["error_code"] == "destructive_change_requires_command"
    assert mgr.state.contract.verification == "security audit passes"


def test_revision_refuses_a_source_message_too_long_to_judge(hermes_home):
    db = _db("rev-long")
    mgr = GoalManager(session_id="rev-long")
    mgr.set("Ship X", contract=GoalContract(verification="security audit passes"))
    message = "x " * 3000 + "Drop the security audit requirement."
    db.append_message("rev-long", "user", message)
    result = mgr.revise(reason="r", contract={"verification": "tests pass"},
                        user_quote="Drop the security audit requirement")
    assert result["error_code"] == "destructive_change_requires_command"
    assert mgr.state.contract.verification == "security audit passes"


def test_evidence_can_supersede_an_obsolete_verification_without_user_quote(hermes_home):
    sid = "rev-evidence"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship X", contract=GoalContract(verification="Old gate must pass."))
    _tool(db, sid, "terminal", {"command": "ci"}, "Old gate must pass. Result superseded by signed release.", "evidence")
    result = mgr.revise(reason="the old gate is obsolete", contract={"verification": "Signed release exists."},
                        evidence="Result superseded by signed release")
    assert result["error_code"] == "evidence_cannot_retire_verification"
    assert load_goal(sid).contract.verification == "Old gate must pass."
    assert mgr.is_active()


def test_evidence_cannot_change_objective_or_constraints(hermes_home):
    sid = "rev-evidence-scope"
    db = _db(sid)
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship X", contract=GoalContract(constraints="No downtime."))
    _tool(db, sid, "terminal", {"command": "ci"}, "signed result", "evidence")
    result = mgr.revise(reason="evidence", contract={"constraints": ""}, evidence="signed result")
    assert result["error_code"] == "destructive_change_requires_command"
    assert load_goal(sid).contract.constraints == "No downtime."


def test_agent_replace_requires_explicit_command(hermes_home):
    mgr = GoalManager(session_id="replace-command")
    mgr.set("Ship X")
    before = load_goal("replace-command").to_json()
    refused = mgr.replace(reason="model request", goal="Ship Y")
    assert refused["error_code"] == "destructive_change_requires_command"
    assert load_goal("replace-command").to_json() == before
    result = _dispatch(mgr, "replace Ship Y")
    assert not result.error and "Goal replaced" in result.output
    assert load_goal("replace-command").goal == "Ship Y"
    assert mgr.is_active()


def test_explicit_remove_commands_apply_and_are_numbered(hermes_home):
    mgr = GoalManager(session_id="remove-command")
    mgr.set("Ship X", contract=GoalContract(constraints="No downtime.; No secrets.", verification="Run CI."))
    mgr.add_subgoal("also migrate Willow")
    status = _dispatch(mgr, "status")
    assert "Constraints:" in status.output and "- 1. No downtime." in status.output
    assert "Verification:" in status.output and "- 1. Run CI." in status.output
    assert "Subgoals:" in status.output and "- 1. also migrate Willow" in status.output
    assert "Removed constraint 2" in _dispatch(mgr, "remove constraint 2").output
    assert "Removed check 1" in _dispatch(mgr, "remove check 1").output
    assert "Removed subgoal 1" in _dispatch(mgr, "remove subgoal 1").output
    state = load_goal("remove-command")
    assert state.contract.constraints == "No downtime."
    assert state.contract.verification == ""
    assert state.subgoals == []
    assert mgr.is_active()


def test_replace_preserves_exhausted_budget_pause_across_persistence(hermes_home):
    mgr = GoalManager(session_id="replace-budget")
    mgr.set("Old objective", max_turns=2)
    mgr.state.turns_used = 2
    mgr._save()   # the evaluator commits spent turns; pause() only edits lifecycle on the live row
    mgr.pause(reason="turn budget exhausted (2/2)")

    result = _dispatch(mgr, "replace New objective")
    assert not result.error
    persisted = load_goal("replace-budget")
    assert persisted.status == "paused"
    assert persisted.paused_reason == "turn budget exhausted (2/2)"
    assert persisted.turns_used == 2 and persisted.max_turns == 2
    assert GoalManager("replace-budget").next_continuation_prompt() is None


@pytest.mark.parametrize("field,kind", [("constraints", "constraint"), ("verification", "check")])
@pytest.mark.parametrize("separator,suffix", [("; ", ""), (". ", "."), ("\n", "")])
@pytest.mark.parametrize("index", [1, 2, 3])
def test_remove_round_trips_exact_remaining_items(hermes_home, field, kind, separator, suffix, index):
    sid = "remove-round-trip"
    original = ["No downtime", "No secrets", "Keep backups"]
    expected = [item + suffix for item in original]
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship X", contract=GoalContract(**{field: separator.join(original) + suffix}))

    result = _dispatch(mgr, f"remove {kind} {index}")
    assert not result.error
    expected.pop(index - 1)
    for remaining in range(len(expected), 0, -1):
        mgr = GoalManager(session_id=sid)
        numbered = [line for line in _dispatch(mgr, "status").output.splitlines() if line.startswith("- ")]
        assert numbered == [f"- {i}. {item}" for i, item in enumerate(expected, 1)]
        assert goals.contract_items(field, getattr(load_goal(sid).contract, field)) == expected
        assert not _dispatch(mgr, f"remove {kind} 1").error
        expected.pop(0)
    assert getattr(load_goal(sid).contract, field) == ""


def test_remove_retires_atomic_multi_sentence_subgoal(hermes_home, monkeypatch):
    sid = "remove-atomic-subgoal"
    mgr = GoalManager(session_id=sid)
    mgr.set("Ship X")
    mgr.add_subgoal("Ship CLI. Add tests.")
    assert mgr.revise(reason="learned a criterion", subgoals=["Ship CLI. Add tests.", "Document it"])["ok"]
    assert "- 1. Ship CLI. Add tests." in _dispatch(mgr, "show").output
    assert not _dispatch(mgr, "remove subgoal 1").error

    mgr = GoalManager(session_id=sid)
    assert load_goal(sid).subgoals == ["Document it"]
    assert "Ship CLI" not in mgr.next_continuation_prompt()
    prompts = _capture(monkeypatch, ['{"verdict":"continue","reason":"Document it"}'])
    assert mgr.evaluate_after_turn("working")["should_continue"]
    assert "Ship CLI" not in prompts[0] and "Add tests" not in prompts[0]


@pytest.mark.parametrize("paused,max_turns", [(False, 20), (False, 0), (True, 2)])
def test_replace_resets_objective_state_without_granting_budget(hermes_home, monkeypatch, paused, max_turns):
    sid = "replace-objective-state"
    mgr = GoalManager(session_id=sid)
    mgr.set("Old objective", max_turns=max_turns)
    if paused:
        mgr.pause(reason="turn budget exhausted (2/2)")
    state = mgr.state
    state.turns_used = 2
    state.consecutive_no_progress = 2
    state.backoff_level = 2
    state.last_progress_fingerprint = "old-proof"
    state.consecutive_parse_failures = 2
    state.consecutive_transport_failures = 2
    state.last_verdict = "blocked" if paused else "continue"
    state.last_reason = "Old criterion still missing"
    mgr._save()

    mgr = GoalManager(session_id=sid)
    assert not _dispatch(mgr, "replace New objective").error
    state = load_goal(sid)
    assert state.goal == "New objective"
    assert state.turns_used == 2 and state.max_turns == max_turns
    assert state.status == ("paused" if paused else "active")
    assert state.paused_reason == ("turn budget exhausted (2/2)" if paused else None)
    assert state.consecutive_no_progress == state.backoff_level == 0
    assert state.consecutive_parse_failures == state.consecutive_transport_failures == 0
    assert state.last_progress_fingerprint is None
    assert state.last_verdict is None and state.last_reason is None

    mgr = GoalManager(session_id=sid)
    if paused:
        assert not mgr.resume_for_user_input()
        assert mgr.next_continuation_prompt() is None
    else:
        _capture(monkeypatch, ['{"verdict":"continue","reason":"New criterion missing"}'])
        assert mgr.evaluate_after_turn("Done.")["should_continue"]
        assert load_goal(sid).last_reason == "New criterion missing"


def test_subgoal_entries_are_atomic_for_additive_diff(hermes_home):
    mgr = GoalManager(session_id="atomic-subgoal-diff")
    mgr.set("Ship X")
    mgr.add_subgoal("Ship CLI. Add tests.")
    before = load_goal(mgr.session_id).to_json()
    result = mgr.revise(reason="split entry", subgoals=["Ship CLI.", "Add tests."])
    assert result["error_code"] == "destructive_change_requires_command"
    assert load_goal(mgr.session_id).to_json() == before


def test_explicit_remove_out_of_range_fails_cleanly(hermes_home):
    mgr = GoalManager(session_id="remove-range")
    mgr.set("Ship X", contract=GoalContract(constraints="No downtime."))
    result = _dispatch(mgr, "remove constraint 2")
    assert result.error and "out of range" in result.output
    assert load_goal("remove-range").contract.constraints == "No downtime."
    assert mgr.is_active()
