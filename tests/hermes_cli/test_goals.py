"""Tests for hermes_cli/goals.py — persistent cross-turn goals."""

from __future__ import annotations

import json
import time
from unittest.mock import patch, MagicMock

import pytest


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so SessionDB.state_meta writes don't clobber the real one."""
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Bust the goal-module's DB cache for each test so it re-resolves HERMES_HOME.
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


# ──────────────────────────────────────────────────────────────────────
# _parse_judge_response
# ──────────────────────────────────────────────────────────────────────


class TestParseJudgeResponse:
    def test_clean_json_done(self):
        from hermes_cli.goals import _parse_judge_response

        verdict, reason, _pf, wait = _parse_judge_response('{"done": true, "reason": "all good"}')
        assert verdict == "done"
        assert reason == "all good"
        assert wait is None


    def test_wait_verdict_with_pid(self):
        from hermes_cli.goals import _parse_judge_response

        v, reason, pf, wait = _parse_judge_response(
            '{"verdict": "wait", "wait_on_pid": 4242, "reason": "CI running"}'
        )
        assert v == "wait"
        assert pf is False
        assert wait == {"pid": 4242}
        assert reason == "CI running"


class TestWaitingDecision:
    @pytest.mark.parametrize(
        ("wait_fields", "expected_reason"),
        [
            ({"waiting_on_session": "worker-session-7"}, "session worker-session-7"),
            ({"waiting_on_pid": 4242}, "pid 4242"),
            ({"waiting_until": 1030.0}, "30s remaining"),
        ],
    )
    def test_wait_notice_reason_keeps_readable_target_when_waiting_reason_missing(
        self, hermes_home, monkeypatch, wait_fields, expected_reason,
    ):
        from hermes_cli.goals import GoalManager, GoalState

        monkeypatch.setattr("hermes_cli.goals.time.time", lambda: 1000.0)
        manager = GoalManager(session_id="wait-reason-sid")
        manager._save = lambda: None
        state = GoalState(goal="wait for the process", waiting_reason=None, **wait_fields)

        decision = manager._waiting_decision(state)

        assert decision["reason"] == expected_reason
        assert state.last_wait_notice_key == manager._wait_notice_key(state)
        repeated = manager._waiting_decision(state)
        assert repeated["reason"] == expected_reason
        assert repeated["message"] == ""


# ──────────────────────────────────────────────────────────────────────
# judge_goal — fail-open semantics
# ──────────────────────────────────────────────────────────────────────


class TestJudgeGoal:


    def test_api_error_continues(self):
        """Judge exception → fail-open continue (don't wedge progress on judge bugs)."""
        from hermes_cli import goals

        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=RuntimeError("boom"),
        ):
            verdict, reason, _, _wd, _tf = goals.judge_goal("goal", "response")
        assert verdict == "continue"
        assert "judge error" in reason.lower()

    def test_judge_says_done(self):
        from hermes_cli import goals

        with patch(
            "agent.auxiliary_client.call_llm",
            return_value=MagicMock(
                choices=[MagicMock(message=MagicMock(content='{"done": true, "reason": "achieved"}'))]
            ),
        ):
            verdict, reason, _, _wd, _tf = goals.judge_goal("goal", "agent response")
        assert verdict == "done"
        assert reason == "achieved"


# ──────────────────────────────────────────────────────────────────────
# GoalManager lifecycle + persistence
# ──────────────────────────────────────────────────────────────────────


class TestGoalManager:

    @pytest.mark.parametrize(
        "goal,contract,code",
        [
            ("x " * 301, None, "length"),
            ("Ship the release", {"verification": "PR #551 is merged"}, "stale_identifier"),
            ("Delete the obsolete topic", {"constraints": "Read-only; no changes"}, "readonly_mutation"),
            ("Ship the release", {"verification": "1. Run tests\n2. Read logs\n3. Review output"}, "verification_checklist"),
            ("Ship the release", {"verification": "Fixes, flood evidence, restart logs, and closed or assigned plan items read back."}, "verification_checklist"),
        ],
    )
    def test_goal_authoring_rules_have_deterministic_findings(self, goal, contract, code):
        from hermes_cli.goals import goal_authoring_findings

        findings = goal_authoring_findings(goal, contract)
        assert [finding.code for finding in findings] == [code]

    @pytest.mark.parametrize("goal", [
        "Run 50 evals",
        "Write docs for job 42 scheduling",
        "Keep build 2026 green",
        "Migrate the cafe5678 table",
        "~/plans/a1b2c3d.md",
        "The dashboard is green, the alert is quiet, and logs are clean.",
    ])
    def test_goal_authoring_allowed_numeric_labels_and_live_outcome(self, goal):
        from hermes_cli.goals import goal_authoring_findings
        assert goal_authoring_findings(goal) == []

    @pytest.mark.parametrize("goal", [
        "Finish scoring the curated source lists using Brian's Round 3 calibration decisions, with the scoring skill deployed. Plan: ~/Workspace/projects/source-scoring/plan.md\nverify: Every list meets the calibration threshold or is marked for rework, and the deployed skill scores a fresh list.\nconstraints: Don't add, remove, or reclassify list entries.\nstop when: Brian's Round 3 calibration decisions are needed.",
        "The credential broker is retired and agents use 1Password directly through service accounts.\nverify: Agents read secrets from each non-Private vault without the broker, and no broker process or config remains.\nconstraints: Never print secrets, rotate credentials without Brian, or touch LPG or Meridian infrastructure.\nstop when: Brian needs to approve the service accounts on his Mac.",
    ])
    def test_goal_authoring_approved_shapes_are_clean(self, goal):
        from hermes_cli.goals import parse_contract, goal_authoring_findings
        headline, contract = parse_contract(goal)
        assert goal_authoring_findings(headline, contract) == []

    def test_goal_authoring_revision_cap_preserves_legacy_small_fix(self):
        from hermes_cli.goals import goal_authoring_findings
        current = len("Keep the service healthy") + len("The service is healthy.")
        assert goal_authoring_findings(
            "Keep the service healthy", {"verification": "x " * 400},
            previous_total=current, changed_fields={"verification"},
        )[0].code == "length"
        assert goal_authoring_findings(
            "Keep the service healthy", {"constraints": "legacy " * 100, "verification": "X is live"},
            previous_total=700, changed_fields={"verification"},
        ) == []

    def test_goal_authoring_revision_checks_cross_field_contradictions(self):
        from hermes_cli.goals import goal_authoring_findings

        findings = goal_authoring_findings(
            "Delete the obsolete topic",
            {"constraints": "Read-only; no changes", "verification": "topic is gone"},
            previous_total=100,
            changed_fields={"goal"},
        )
        assert [finding.code for finding in findings] == ["readonly_mutation"]
        assert goal_authoring_findings(
            "Keep the service healthy",
            {"constraints": "Read-only; no changes", "verification": "Verify no deploys occurred"},
        ) == []

    def test_goal_authoring_checks_waits_in_outcome_and_not_measurement_windows(self):
        from hermes_cli.goals import goal_authoring_findings

        findings = goal_authoring_findings(
            "Keep the service healthy", {"outcome": "Wait for 24 hours before declaring success"}
        )
        assert [finding.code for finding in findings] == ["fixed_wait"]
        assert goal_authoring_findings(
            "Keep the service healthy", {"verification": "The service remains healthy and the report covers the last 7 days."}
        ) == []

    def test_goal_authoring_rejects_explicit_wait_over_a_measurement_window(self):
        from hermes_cli.goals import goal_authoring_findings

        findings = goal_authoring_findings("Wait for the last 7 days before declaring success")
        assert [finding.code for finding in findings] == ["fixed_wait"]

    def test_goal_authoring_rejects_fixed_wait_in_goal(self):
        from hermes_cli.goals import goal_authoring_findings

        findings = goal_authoring_findings("Wait for 24 hours before declaring success")
        assert [finding.code for finding in findings] == ["fixed_wait"]
        findings = goal_authoring_findings("Wait 24 hours before declaring success")
        assert [finding.code for finding in findings] == ["fixed_wait"]

    @pytest.mark.parametrize("goal,constraints", [
        ("Deploy the new API", "No deletions of user data"),
        ("Ship the fix", "Don't merge without Brian's approval"),
        ("Write the weekly report", "No sends to clients"),
        ("Create the migration plan", "Do not deploy to production"),
    ])
    def test_goal_authoring_allows_scoped_hard_limits(self, goal, constraints):
        from hermes_cli.goals import goal_authoring_findings
        assert goal_authoring_findings(goal, {"constraints": constraints}) == []

    @pytest.mark.parametrize("goal,constraints", [
        ("Modify the service", "read-only; no changes"),
        ("Deploy the new API", "No deploys."),
        ("Remove the stale topic", "Never delete"),
    ])
    def test_goal_authoring_flags_limits_that_forbid_the_outcome(self, goal, constraints):
        from hermes_cli.goals import goal_authoring_findings
        findings = goal_authoring_findings(goal, {"constraints": constraints})
        assert [finding.code for finding in findings] == ["readonly_mutation"]

    @pytest.mark.parametrize("goal,outcome", [
        ("Audit recent config changes", ""),
        ("Find which commit added the regression", ""),
        ("Investigate why the deploy failed", ""),
        ("Explain the update flow", ""),
        ("Write up the root cause of the outage", ""),
        ("Compare three CRMs", "Brian has a written comparison of three CRMs"),
        ("Review the goal-authoring change", "Exact-head review returns a verdict"),
        ("Report which topics were sent to the wrong chat", ""),
        ("Determine whether the topic was deleted", ""),
        ("Check if the migration is deployed", ""),
        ("Explain why updates are sent late", ""),
        ("Verify that the backup is written nightly", ""),
        ("List files that were removed last week", ""),
        ("Identify what should be deleted", ""),
        ("Check whether the migration is deployed and the backup is written", ""),
        ("Report which topics are sent and are deleted", ""),
    ])
    def test_goal_authoring_allows_read_only_investigations(self, goal, outcome):
        from hermes_cli.goals import goal_authoring_findings
        contract = {"constraints": "Read-only; no changes"}
        if outcome:
            contract["outcome"] = outcome
        assert goal_authoring_findings(goal, contract) == []

    @pytest.mark.parametrize("goal,contract", [
        ("The obsolete topic is deleted", {"constraints": "read-only"}),
        ("Fix the config and deploy", {"constraints": "No deploys."}),
        ("Clean up stale topics", {"outcome": "Stale topics are removed", "constraints": "Never delete"}),
        ("Draft the summary, then send it", {"constraints": "no changes"}),
        ("Report which topics were sent, then the summary is sent", {"constraints": "no changes"}),
    ])
    def test_goal_authoring_flags_required_end_states_and_later_clauses(self, goal, contract):
        from hermes_cli.goals import goal_authoring_findings
        assert [finding.code for finding in goal_authoring_findings(goal, contract)] == ["readonly_mutation"]

    def test_goal_authoring_unrelated_revision_skips_legacy_contradiction(self):
        from hermes_cli.goals import goal_authoring_findings
        assert goal_authoring_findings(
            "Delete the topic", {"constraints": "Read-only", "verification": "topic gone"},
            previous_total=50, changed_fields={"verification"},
        ) == []

    @pytest.mark.parametrize("verification", [
        "The service remains healthy over the last 24 hours.",
        "The service remains healthy and the 24-hour error rate is below 1%.",
        "Monitoring shows the 7-day error budget intact.",
        "Errors remain below 1% for the past 2 weeks.",
    ])
    def test_goal_authoring_allows_hour_and_week_measurement_windows(self, verification):
        from hermes_cli.goals import goal_authoring_findings
        assert goal_authoring_findings("Keep the service healthy", {"verification": verification}) == []

    def test_goal_authoring_flags_after_duration_waits(self):
        from hermes_cli.goals import goal_authoring_findings
        findings = goal_authoring_findings(
            "Keep the service healthy", {"verification": "Check the dashboard again after 24 hours."}
        )
        assert [finding.code for finding in findings] == ["fixed_wait"]

    def test_goal_authoring_length_finding_names_the_contract(self):
        from hermes_cli.goals import goal_authoring_findings
        findings = goal_authoring_findings("Keep X healthy", {"constraints": "y " * 400})
        assert ("length", "contract") in [(finding.code, finding.field) for finding in findings]

    @pytest.mark.parametrize("goal", [
        "Resolve 0xble/agents#435",
        "Fix importer per ~/plans/pr#12/plan.md until issue 42 is closed",
    ])
    def test_goal_authoring_path_exemption_keeps_real_references(self, goal):
        from hermes_cli.goals import goal_authoring_findings
        assert "stale_identifier" in [finding.code for finding in goal_authoring_findings(goal)]

    def test_set_then_status(self, hermes_home):
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="test-sid-2", default_max_turns=5)
        state = mgr.set("port the thing")
        assert state.goal == "port the thing"
        assert state.status == "active"
        assert state.max_turns == 5
        assert state.turns_used == 0
        assert mgr.is_active()
        assert "active" in mgr.status_line().lower()
        assert "port the thing" in mgr.status_line()

    def test_zero_budget_is_persistent_unlimited_and_continues(self, hermes_home):
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager, load_goal

        mgr = GoalManager(session_id="unlimited-sid", default_max_turns=0)
        state = mgr.set("keep going")
        assert state.max_turns == 0
        assert load_goal("unlimited-sid").max_turns == 0
        assert "∞" in mgr.status_line()

        with patch.object(goals, "judge_goal", return_value=("continue", "more work", False, None, False)):
            for i in range(101):
                decision = mgr.evaluate_after_turn(f"step {i}")
                assert decision["should_continue"] is True
        assert mgr.state.status == "active"
        assert mgr.state.turns_used == 101
        assert load_goal("unlimited-sid").turns_used == 101

    def test_finite_budget_still_pauses(self, hermes_home):
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="finite-sid", default_max_turns=2)
        mgr.set("stop at two")
        with patch.object(goals, "judge_goal", return_value=("continue", "more work", False, None, False)):
            assert mgr.evaluate_after_turn("step 1")["should_continue"] is True
            decision = mgr.evaluate_after_turn("step 2")
        assert decision["status"] == "paused"
        assert mgr.state.paused_reason == "turn budget exhausted (2/2)"

    @pytest.mark.parametrize(
        ("reason", "expected_status", "waiting_on_user", "paused"),
        [
            ("must not be merged until Brian approves", "active", True, False),
            ("the release must not be merged until Brian approves", "active", True, False),
            ("already merged despite the constraint", "paused", False, True),
            ("already merged even though it must not have been", "paused", False, True),
        ],
    )
    def test_blocked_approval_waits_but_completed_constraint_breach_pauses(
        self, hermes_home, reason, expected_status, waiting_on_user, paused,
    ):
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id=f"blocked-classification-{expected_status}")
        mgr.set("ship the release")
        with patch.object(goals, "judge_goal", return_value=("blocked", reason, False, None, False)):
            decision = mgr.evaluate_after_turn("blocked")

        assert decision["status"] == expected_status
        assert mgr.state is not None
        assert mgr.state.status == expected_status
        assert mgr.state.waiting_on_user_message is waiting_on_user
        assert (mgr.state.paused_reason is not None) is paused

    def test_unlimited_budget_keeps_lifecycle_controls_functional(self, hermes_home):
        """Zero is only a budget sentinel: terminal and user lifecycle controls still work."""
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager, load_goal

        done = GoalManager(session_id="unlimited-done", default_max_turns=0)
        done.set("finish without a budget")
        done.mark_done("verified")
        assert done.state.status == "done"
        assert load_goal("unlimited-done").status == "done"

        paused = GoalManager(session_id="unlimited-paused", default_max_turns=0)
        paused.set("pause and resume")
        paused.pause("user-paused")
        assert paused.state.status == "paused"
        paused.resume()
        assert paused.state.status == "active"
        assert paused.state.max_turns == 0

        blocked = GoalManager(session_id="unlimited-blocked", default_max_turns=0)
        blocked.set("needs input")
        with patch.object(
            goals, "judge_goal", return_value=("blocked", "needs user input", False, None, False)
        ):
            decision = blocked.evaluate_after_turn("blocked")
        # Needing the user is a user-message wait, not a pause.
        assert decision["status"] == "active"
        assert blocked.state.waiting_on_user_message is True
        blocked.clear()
        assert blocked.state is None
        assert load_goal("unlimited-blocked").status == "cleared"


    def test_continuation_prompt_shape(self, hermes_home):
        """The continuation prompt must include the goal text verbatim —
        and must be safe to inject as a user-role message (prompt-cache
        invariants: no system-prompt mutation)."""
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="cont-sid")
        mgr.set("port goal command to hermes")
        prompt = mgr.next_continuation_prompt()
        assert prompt is not None
        assert "port goal command to hermes" in prompt
        assert prompt.strip()  # non-empty

    def test_user_authorized_replacement_supersedes_all_prior_requirements_in_prompts(self, hermes_home):
        """A user replacement starts a new outcome scope, including prior agent revisions."""
        from hermes_cli.goals import GoalContract, GoalManager, judge_goal

        manager = GoalManager(session_id="replacement-boundary")
        manager.set(
            "migrate production",
            contract=GoalContract(verification="production migration verified"),
        )
        manager.add_subgoal("production shard")
        revised = manager.revise(
            reason="tighten the verification wording",
            contract={"verification": "production migration fully verified"},
        )
        assert revised["error_code"] == "destructive_change_requires_command"

        replaced = manager.replace(
            reason="the user changed the outcome",
            goal="write a local design note",
            contract=GoalContract(verification="design note exists"),
            actor="user",
        )
        assert replaced["ok"] is True

        continuation = manager.next_continuation_prompt()
        assert continuation is not None
        assert "write a local design note" in continuation
        assert manager.state is not None

        with patch(
            "agent.auxiliary_client.call_llm",
            return_value=MagicMock(
                choices=[MagicMock(message=MagicMock(content='{"verdict": "continue", "reason": "more"}'))]
            ),
        ) as call_llm:
            judge_goal(manager.state.goal, "still working", contract=manager.state.contract,
                       subgoals=manager.state.subgoals)
        judge_prompt = call_llm.call_args.kwargs["messages"][1]["content"]
        assert "design note exists" in judge_prompt
        assert (
            "When the goal's boundaries or constraints forbid demonstrating a case live, recorded "
            "test evidence for that case satisfies the criterion if the tool output names the tests and "
            "includes a pass count."
        ) in judge_prompt

    def test_continuation_prompt_silences_unchanged_release_rechecks(self, hermes_home):
        from hermes_cli.goals import GoalManager

        manager = GoalManager(session_id="unchanged-release-recheck")
        manager.set("keep the finished result")
        prompt = manager.next_continuation_prompt()
        assert prompt is not None
        assert (
            "A restart, update, or new release by itself is not news, and re-running a check that returns "
            "the same result as your last visible report counts as unchanged; reply with exactly [SILENT] "
            "and nothing else."
        ) in prompt

    def test_continuation_prompts_share_silence_rule_but_gate_failure_does_not(self, hermes_home):
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager

        manager = GoalManager(session_id="prompt-variants")
        manager.set("finish the task")
        assert goals.SILENCE_MARKER in manager.next_continuation_prompt()

        manager._state.contract = goals.GoalContract(outcome="ship it")
        assert goals.SILENCE_MARKER in manager.next_continuation_prompt()

        manager._state.contract = None
        manager._state.subgoals = ["also verify it"]
        assert goals.SILENCE_MARKER in manager.next_continuation_prompt()

        gate = goals.CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE.format(
            goal="finish the task", command="pytest", exit_code=1, attempt=1, max_retries=2,
            output="failed",
        )
        assert goals.SILENCE_MARKER not in gate

    def test_silent_automatic_continuation_rejudges_and_counts_no_progress(self, hermes_home):
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager

        manager = GoalManager(session_id="silent-continuation", default_max_turns=20)
        manager.set("wait for an external result")
        with patch.object(
            goals, "judge_goal", return_value=("continue", "still waiting", False, None, False),
        ) as judge:
            decision = manager.evaluate_after_turn(goals.SILENCE_MARKER, user_initiated=False)
        judge.assert_called_once()
        assert judge.call_args.args[1] == goals.SILENCE_MARKER
        # One automatic no-progress turn parks quietly on the first ladder rung.
        assert decision["should_continue"] is False
        assert decision["status"] == "active"
        assert decision["message"] == ""
        assert manager.state.consecutive_parse_failures == 0
        assert manager.state.consecutive_no_progress == 1
        assert manager.state.waiting_seconds == 15 * 60


# ──────────────────────────────────────────────────────────────────────
# Smoke: CommandDef is wired
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
# Auto-pause on consecutive judge parse failures
# ──────────────────────────────────────────────────────────────────────


class TestJudgeParseFailureAutoPause:
    """Weak judge models (e.g. deepseek-v4-flash) that return empty strings or
    non-JSON prose never pause the goal; failures are counted as diagnostics only."""


    def test_api_error_does_not_count_as_parse_failure(self):
        """Transient network/API errors must not trip the auto-pause guard."""
        from hermes_cli import goals

        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=RuntimeError("connection reset"),
        ):
            verdict, _, parse_failed, _wd, transport_failed = goals.judge_goal(
                "goal", "response"
            )
        assert verdict == "continue"
        assert parse_failed is False
        assert transport_failed is True


    def test_parse_failures_never_pause_and_continue_silently(self, hermes_home):
        """Unparseable judge output is a model-quality problem: the goal keeps going silently."""
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="parse-fail-sid-1", default_max_turns=20)
        mgr.set("do a thing")

        with patch.object(
            goals, "judge_goal", return_value=("continue", "judge returned empty response", True, None, False)
        ):
            d1 = mgr.evaluate_after_turn("step 1")
            assert d1["should_continue"] is True
            assert mgr.state.consecutive_parse_failures == 1

            d2 = mgr.evaluate_after_turn("step 2")
            assert d2["should_continue"] is True
            assert mgr.state.consecutive_parse_failures == 2

            for step in range(3, 6):
                d = mgr.evaluate_after_turn(f"step {step}")
                assert d["should_continue"] is True
                assert d["status"] == "active"
                assert d["message"] == ""
            assert mgr.state.consecutive_parse_failures == 5
            assert mgr.state.status == "active"


# ──────────────────────────────────────────────────────────────────────
# /subgoal — user-added criteria
# ──────────────────────────────────────────────────────────────────────


class TestGoalStateSubgoalsBackcompat:
    def test_old_state_meta_row_loads_without_subgoals(self):
        """A goal serialized BEFORE the subgoals field existed must
        round-trip with an empty list, not crash."""
        from hermes_cli.goals import GoalState

        legacy = json.dumps({
            "goal": "do a thing",
            "status": "active",
            "turns_used": 2,
            "max_turns": 20,
            "created_at": 1.0,
            "last_turn_at": 2.0,
            "consecutive_parse_failures": 0,
        })
        state = GoalState.from_json(legacy)
        assert state.goal == "do a thing"
        assert state.subgoals == []


class TestMigrateGoalToSession:
    """migrate_goal_to_session carries a /goal from a parent session to its
    compression continuation child (#33618). load_goal does a flat
    per-session lookup with no lineage walk, so without migration an active
    goal silently dies when compression rotates session_id."""

    def test_migrates_active_goal_to_child(self, hermes_home):
        from hermes_cli.goals import save_goal, load_goal, migrate_goal_to_session, GoalState
        save_goal("parent-sid", GoalState(goal="ship the feature"))
        assert migrate_goal_to_session("parent-sid", "child-sid", reason="compression") is True
        child = load_goal("child-sid")
        assert child is not None and child.goal == "ship the feature"
        # Parent row archived (cleared) so only the child is active.
        parent = load_goal("parent-sid")
        assert parent is not None and parent.status == "cleared"


    def test_does_not_clobber_existing_child_goal(self, hermes_home):
        from hermes_cli.goals import save_goal, load_goal, migrate_goal_to_session, GoalState
        save_goal("p3", GoalState(goal="parent goal"))
        save_goal("c3", GoalState(goal="child already has one"))
        assert migrate_goal_to_session("p3", "c3") is False
        assert load_goal("c3").goal == "child already has one"


class TestSessionDbCacheAfterProfileDelete:
    """``hermes profile delete`` force-closes every registry handle under the profile home
    (``close_all_under``) and rmtrees it; recreating the same name in the long-lived dashboard
    process must not keep persisting goals into the torn-down handle."""

    def test_delete_then_recreate_gets_a_live_store(self, hermes_home):
        import shutil

        import hermes_state
        import hermes_state_registry as registry
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.goals import GoalState, _get_session_db, load_goal, save_goal

        # conftest re-points DEFAULT_DB_PATH at one fixed file; the registry must resolve the
        # scoped profile home here, as production does.
        with patch.object(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH):
            profile = hermes_home / "profiles" / "p1"
            profile.mkdir(parents=True)
            token = set_hermes_home_override(profile)
            try:
                save_goal("s1", GoalState(goal="before delete"))
                stale = _get_session_db()
                assert registry.close_all_under(profile) == 1
                shutil.rmtree(profile)
                profile.mkdir(parents=True)

                save_goal("s2", GoalState(goal="after recreate"))

                assert _get_session_db() is not stale
                assert (profile / "state.db").exists()
                assert load_goal("s2").goal == "after recreate"
            finally:
                reset_hermes_home_override(token)
                registry.close_all_under(profile)


class TestGoalManagerSubgoals:
    def test_add_subgoal(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="sub-add")
        mgr.set("main goal")
        text = mgr.add_subgoal("  use bullet points  ")
        assert text == "use bullet points"
        assert mgr.state.subgoals == ["use bullet points"]


    def test_remove_subgoal_out_of_range(self, hermes_home):
        import pytest
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="sub-oob")
        mgr.set("g")
        mgr.add_subgoal("only")
        with pytest.raises(IndexError):
            mgr.remove_subgoal(5)
        with pytest.raises(IndexError):
            mgr.remove_subgoal(0)


class TestJudgeGoalWithSubgoals:
    def test_judge_uses_subgoals_template_when_provided(self, hermes_home):
        """judge_goal switches templates when subgoals is non-empty.

        We don't actually call the model — we patch the aux client to
        capture the prompt that would be sent.
        """
        from unittest.mock import patch
        from hermes_cli import goals

        captured = {}

        class _FakeMsg:
            content = '{"done": true, "reason": "all done"}'
        class _FakeChoice:
            message = _FakeMsg()
        class _FakeResp:
            choices = [_FakeChoice()]
        def _fake_call_llm(**kwargs):
            captured.update(kwargs)
            return _FakeResp()

        with patch("agent.auxiliary_client.call_llm", side_effect=_fake_call_llm):
            verdict, _reason, _parse_failed, _wd, _tf = goals.judge_goal(
                "ship the feature",
                "ok shipped",
                subgoals=["write tests", "update docs"],
            )

        # The aux client was called with a prompt that includes the subgoals.
        sent_messages = captured.get("messages") or []
        user_msg = next((m["content"] for m in sent_messages if m["role"] == "user"), "")
        assert "Additional criteria" in user_msg
        assert "1. write tests" in user_msg
        assert "2. update docs" in user_msg
        assert verdict == "done"

    def test_judge_uses_original_template_when_no_subgoals(self, hermes_home):
        from unittest.mock import patch
        from hermes_cli import goals

        captured = {}

        class _FakeMsg:
            content = '{"done": true, "reason": "ok"}'
        class _FakeChoice:
            message = _FakeMsg()
        class _FakeResp:
            choices = [_FakeChoice()]
        def _fake_call_llm(**kwargs):
            captured.update(kwargs)
            return _FakeResp()

        with patch("agent.auxiliary_client.call_llm", side_effect=_fake_call_llm):
            goals.judge_goal("ship it", "done", subgoals=None)

        sent_messages = captured.get("messages") or []
        user_msg = next((m["content"] for m in sent_messages if m["role"] == "user"), "")
        assert "Additional criteria" not in user_msg
        assert "ship it" in user_msg


# ──────────────────────────────────────────────────────────────────────
# Wait barrier — parking the goal loop on a background process
# ──────────────────────────────────────────────────────────────────────


class TestWaitBarrier:
    """The /goal wait barrier parks the loop on a live PID and resumes when
    the process exits, without burning turns or calling the judge."""

    @staticmethod
    def _spawn_sleeper():
        """Start a short-lived child process; return its Popen handle."""
        import subprocess
        import sys
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    @staticmethod
    def _dead_pid():
        """A PID that is essentially guaranteed not to be running."""
        return 2_000_000_000


    def test_parked_on_live_pid_does_not_continue_or_judge(self, hermes_home):
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager

        proc = self._spawn_sleeper()
        try:
            mgr = GoalManager(session_id="wb-live")
            mgr.set("ship it", max_turns=5)
            mgr.wait_on(proc.pid, reason="CI green")
            assert mgr.is_waiting() is True

            # The judge must NOT be called on an automatic turn while parked, and no turn is burned.
            judge = MagicMock(return_value=("continue", "x", False, None, False))
            with patch.object(goals, "judge_goal", judge):
                decision = mgr.evaluate_after_turn("still waiting on CI", user_initiated=False)

            judge.assert_not_called()
            assert decision["verdict"] == "waiting"
            assert decision["should_continue"] is False
            assert decision["continuation_prompt"] is None
            assert mgr.state.turns_used == 0  # no turn consumed while parked
            assert "CI green" in decision["message"]
            assert mgr.state.status == "active"  # still active, just parked

            # A real user message pierces the live-pid wait and re-judges.
            with patch.object(goals, "judge_goal", judge):
                pierced = mgr.evaluate_after_turn("user asked for status", user_initiated=True)
            judge.assert_called_once()
            assert pierced["verdict"] == "continue"
            assert mgr.state.waiting_on_pid is None
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_wait_on_rejects_a_pid_not_alive_on_this_host(self, hermes_home, monkeypatch):
        """Regression for #110826: do not persist a barrier for remote/dead PIDs."""
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager

        monkeypatch.setattr(goals, "_pid_alive", lambda pid: False)
        mgr = GoalManager(session_id="wb-dead")
        mgr.set("ship it")

        with pytest.raises(ValueError, match="not alive on this host"):
            mgr.wait_on(4242, reason="remote CI")

        assert mgr.state.waiting_on_pid is None


    def test_stop_waiting_clears_barrier(self, hermes_home):
        from hermes_cli.goals import GoalManager

        proc = self._spawn_sleeper()
        try:
            mgr = GoalManager(session_id="wb-stop")
            mgr.set("g")
            mgr.wait_on(proc.pid)
            assert mgr.is_waiting() is True
            assert mgr.stop_waiting() is True
            assert mgr.state.waiting_on_pid is None
            assert mgr.is_waiting() is False
            assert mgr.stop_waiting() is False  # idempotent
        finally:
            proc.terminate()
            proc.wait(timeout=10)

class TestGatherBackgroundProcessesOwnership:
    def test_only_the_owning_sessions_processes_are_seen(self, monkeypatch):
        """The registry task_id collapses to one container key for every agent in the process, so a
        fan-out parent's judge must filter by owner; otherwise a grandchild's poller parks the goal."""
        from hermes_cli import goals

        class _Reg:
            def list_sessions(self, task_id=None, session_key=None):
                return [
                    {"session_id": "proc_mine", "status": "running", "owner_task_id": "root-sid", "task_id": "default"},
                    {"session_id": "proc_child", "status": "running", "owner_task_id": "sa-1-abc", "task_id": "default"},
                    {"session_id": "proc_done", "status": "exited", "owner_task_id": "root-sid", "task_id": "default"},
                ]

        import tools.process_registry as pr
        monkeypatch.setattr(pr, "process_registry", _Reg())
        assert [p["session_id"] for p in goals.gather_background_processes()] == ["proc_mine", "proc_child"]
        assert [p["session_id"] for p in goals.gather_background_processes(owner_task_id="root-sid")] == ["proc_mine"]


# ──────────────────────────────────────────────────────────────────────
# Judge-driven auto-wait — the judge parks the loop on its own
# ──────────────────────────────────────────────────────────────────────


class TestJudgeDrivenWait:
    """The judge returns a `wait` verdict (given live background-process
    context) and the loop parks automatically — no manual /goal wait."""

    @staticmethod
    def _spawn_sleeper():
        import subprocess, sys
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    def test_judge_wait_on_dead_pid_uses_floor_instead_of_pid_park(self, hermes_home, monkeypatch):
        """#110826: a stale judge ``wait_on_pid`` target gets a timed floor wait, not a
        forever-reparkable PID barrier."""
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager

        clock = [10_000.0]
        monkeypatch.setattr(goals.time, "time", lambda: clock[0])
        mgr = GoalManager(session_id="jw-dead-pid", default_max_turns=10)
        mgr.set("ship the PR")
        assert mgr.state is not None
        mgr.state.last_automatic_continuation_at = clock[0]
        mgr._save()
        with patch.object(goals, "_pid_alive", return_value=False), patch.object(
            goals, "judge_goal",
            return_value=("wait", "remote job still running", False, {"pid": 4242}, False),
        ):
            decision = mgr.evaluate_after_turn("Started the job over ssh (pid 4242).")
        floor = max(mgr.min_continuation_gap_seconds, goals.DEFAULT_MIN_CONTINUATION_GAP_SECONDS)
        assert decision["verdict"] == "wait"
        assert decision["should_continue"] is False
        assert decision["reason"] == goals._MIN_CONTINUATION_GAP_REASON
        assert mgr.state.waiting_on_pid is None
        assert mgr.state.waiting_reason == goals._MIN_CONTINUATION_GAP_REASON
        assert mgr.state.waiting_until > clock[0]
        assert mgr.state.waiting_until <= clock[0] + floor
        assert mgr.is_waiting() is True

    def test_judge_wait_on_pid_dying_between_check_and_park_uses_floor_wait(self, hermes_home, monkeypatch):
        """If the pid exits between the judge path's liveness probe and ``wait_on``'s re-check,
        the race gets a timed floor wait rather than a forever PID park."""
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager

        clock = [20_000.0]
        monkeypatch.setattr(goals.time, "time", lambda: clock[0])
        mgr = GoalManager(session_id="jw-toctou-pid", default_max_turns=10)
        mgr.set("ship the PR")
        assert mgr.state is not None
        mgr.state.last_automatic_continuation_at = clock[0]
        mgr._save()
        with patch.object(goals, "_pid_alive", return_value=True), patch.object(
            GoalManager, "wait_on", side_effect=ValueError("pid is not alive on this host"),
        ), patch.object(
            goals, "judge_goal",
            return_value=("wait", "job still running", False, {"pid": 4242}, False),
        ):
            decision = mgr.evaluate_after_turn("Started the job (pid 4242).")
        floor = max(mgr.min_continuation_gap_seconds, goals.DEFAULT_MIN_CONTINUATION_GAP_SECONDS)
        assert decision["verdict"] == "wait"
        assert decision["should_continue"] is False
        assert decision["reason"] == goals._MIN_CONTINUATION_GAP_REASON
        assert mgr.state.waiting_on_pid is None
        assert mgr.state.waiting_reason == goals._MIN_CONTINUATION_GAP_REASON
        assert mgr.state.waiting_until > clock[0]
        assert mgr.state.waiting_until <= clock[0] + floor
        assert mgr.is_waiting() is True

    def test_judge_repark_on_same_session_dedupes_notice(self, hermes_home):
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="jw-repark-session")
        mgr.set("ship the PR")
        first = mgr._apply_wait_directive(
            {"session_id": "worker-session-7"}, "CI still running",
        )
        second = mgr._apply_wait_directive(
            {"session_id": "worker-session-7"}, "CI still running",
        )
        assert first["message"]
        assert second["message"] == ""

    def test_judge_repark_on_same_timed_wait_dedupes_notice(self, hermes_home, monkeypatch):
        from hermes_cli.goals import GoalManager

        now = iter((1000.0, 1000.0, 1000.0, 1005.0))
        monkeypatch.setattr(
            "hermes_cli.goals.time.time", lambda: next(now, 1005.0),
        )
        mgr = GoalManager(session_id="jw-repark-timed")
        mgr.set("ship the PR")
        first = mgr._apply_wait_directive(
            {"seconds": 30}, "cooldown", active_delegations=2,
        )
        second = mgr._apply_wait_directive(
            {"seconds": 30}, "cooldown", active_delegations=2,
        )
        assert first["message"]
        assert second["message"] == ""

    def test_judge_repark_change_target_or_reason_reannounces(self, hermes_home):
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="jw-repark-change")
        mgr.set("ship the PR")
        first = mgr._apply_wait_directive({"session_id": "worker-session-7"}, "CI still running")
        different_target = mgr._apply_wait_directive({"session_id": "worker-session-8"}, "CI still running")
        different_reason = mgr._apply_wait_directive({"session_id": "worker-session-8"}, "deploy still running")
        assert first["message"]
        assert different_target["message"]
        assert different_reason["message"]

    def test_judge_repark_after_continue_reannounces(self, hermes_home, monkeypatch):
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager

        monkeypatch.setattr("hermes_cli.goals.time.time", lambda: 1000.0)
        mgr = GoalManager(session_id="jw-repark-resume")
        mgr.set("ship the PR")
        with patch.object(
            goals, "judge_goal",
            side_effect=[
                ("wait", "cooldown", False, {"seconds": 30}, False),
                ("continue", "more work", False, None, False),
                ("wait", "cooldown", False, {"seconds": 30}, False),
            ],
        ):
            first = mgr.evaluate_after_turn("start")
            mgr.state.waiting_until = 999.0
            mgr._save()
            continued = mgr.evaluate_after_turn("deadline passed")
            parked_again = mgr.evaluate_after_turn("more progress")
        assert first["message"]
        assert continued["verdict"] == "continue"
        assert parked_again["message"]

    def test_judge_wait_pid_parks_loop(self, hermes_home):
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager

        proc = self._spawn_sleeper()
        try:
            mgr = GoalManager(session_id="jw-pid", default_max_turns=10)
            mgr.set("ship the PR")
            # Judge sees the running process and says wait-on-pid.
            with patch.object(
                goals, "judge_goal",
                return_value=("wait", "CI watcher still running", False, {"pid": proc.pid}, False),
            ):
                decision = mgr.evaluate_after_turn(
                    "Pushed the PR, watching CI.",
                    background_processes=[{
                        "pid": proc.pid, "command": "wait_for_pr_green.sh",
                        "status": "running", "uptime_seconds": 12,
                    }],
                )
            assert decision["verdict"] == "wait"
            assert decision["should_continue"] is False
            assert decision["continuation_prompt"] is None
            assert mgr.state.waiting_on_pid == proc.pid
            assert mgr.is_waiting() is True

            # Next automatic turn while still parked: judge must NOT be called again.
            judge = MagicMock()
            with patch.object(goals, "judge_goal", judge):
                d2 = mgr.evaluate_after_turn("still going", user_initiated=False)
            judge.assert_not_called()
            assert d2["verdict"] == "waiting"
            assert d2["should_continue"] is False
            assert mgr.state.waiting_on_pid == proc.pid
        finally:
            proc.terminate()
            proc.wait(timeout=10)


    def test_time_barrier_clears_after_deadline(self, hermes_home):
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="jw-deadline")
        mgr.set("g")
        mgr.wait_for_seconds(120, reason="backoff")
        assert mgr.is_waiting() is True
        # Force the deadline into the past → barrier auto-clears.
        mgr.state.waiting_until = time.time() - 1
        assert mgr.is_waiting() is False
        mgr.stop_waiting()
        assert mgr.state.waiting_until == 0.0

    def test_continue_verdict_still_continues_with_background(self, hermes_home):
        """A running process present but judge says continue → normal loop."""
        from hermes_cli import goals
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="jw-cont", default_max_turns=10)
        mgr.set("do work")
        with patch.object(
            goals, "judge_goal",
            return_value=("continue", "more to do", False, None, False),
        ):
            decision = mgr.evaluate_after_turn(
                "made progress",
                background_processes=[{"pid": 999999, "command": "x", "status": "running"}],
            )
        assert decision["verdict"] == "continue"
        assert decision["should_continue"] is True
        assert mgr.state.waiting_on_pid is None


# ──────────────────────────────────────────────────────────────────────
# Session/trigger barrier — wait on a process's OWN trigger, not just exit
# ──────────────────────────────────────────────────────────────────────


class TestSessionTriggerBarrier:
    """The session barrier (wait_on_session) releases when a process's own
    trigger fires — a watch_patterns match mid-run (process may never exit)
    OR exit — not only on PID exit. CI-safe: uses synthetic registry session
    objects, no real child processes."""

    @staticmethod
    def _inject(sid, *, watch_patterns=None, exited=False):
        import time as _t
        from tools.process_registry import process_registry, ProcessSession
        s = ProcessSession(id=sid, command="watcher.sh", task_id="t",
                           session_key="", cwd="/tmp", started_at=_t.time())
        if watch_patterns:
            s.watch_patterns = list(watch_patterns)
        s.exited = exited
        if exited:
            process_registry._finished[sid] = s
        else:
            process_registry._running[sid] = s
        return s, process_registry


    def test_registry_releases_on_watch_match_while_alive(self, hermes_home):
        s, reg = self._inject("proc_t2", watch_patterns=["READY"])
        assert reg.is_session_waiting("proc_t2") is True
        s._watch_hits = 1  # what _check_watch_patterns sets on a match
        # Released even though the process is STILL running (never exited).
        assert s.exited is False
        assert reg.is_session_waiting("proc_t2") is False


    def test_wait_on_session_validation(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="st-val")
        # No active goal → RuntimeError
        try:
            mgr.wait_on_session("proc_x")
            assert False, "expected RuntimeError"
        except RuntimeError:
            pass
        mgr.set("g")
        try:
            mgr.wait_on_session("")
            assert False, "expected ValueError"
        except ValueError:
            pass


# ──────────────────────────────────────────────────────────────────────
# Completion contract (Codex-inspired structured goals)
# ──────────────────────────────────────────────────────────────────────


class TestParseContract:


    def test_inline_fields_parsed(self):
        from hermes_cli.goals import parse_contract

        text = (
            "Migrate auth to JWT\n"
            "verify: the auth test suite passes\n"
            "constraints: keep the /login response shape unchanged\n"
            "boundaries: only touch services/auth and its tests\n"
            "stop when: a schema change needs product sign-off"
        )
        headline, contract = parse_contract(text)
        assert headline == "Migrate auth to JWT"
        assert contract.verification == "the auth test suite passes"
        assert contract.constraints == "keep the /login response shape unchanged"
        assert contract.boundaries == "only touch services/auth and its tests"
        assert contract.stop_when == "a schema change needs product sign-off"
        assert not contract.is_empty()

    def test_alias_variants(self):
        from hermes_cli.goals import parse_contract

        _, c = parse_contract("Goal\nverified by: tests green\npreserve: public API")
        assert c.verification == "tests green"
        assert c.constraints == "public API"


class TestGoalContractSerialization:
    def test_roundtrip_with_contract(self):
        from hermes_cli.goals import GoalState, GoalContract

        state = GoalState(
            goal="ship it",
            contract=GoalContract(
                verification="pytest passes",
                constraints="don't break the API",
            ),
        )
        restored = GoalState.from_json(state.to_json())
        assert restored.goal == "ship it"
        assert restored.contract.verification == "pytest passes"
        assert restored.contract.constraints == "don't break the API"
        assert restored.has_contract()

    def test_old_row_without_contract_loads_clean(self):
        # A state_meta row written before this feature has no "contract" key.
        from hermes_cli.goals import GoalState

        legacy = '{"goal": "old goal", "status": "active", "turns_used": 2}'
        state = GoalState.from_json(legacy)
        assert state.goal == "old goal"
        assert state.turns_used == 2
        assert state.contract.is_empty()
        assert not state.has_contract()

    def test_render_block_omits_empty_fields(self):
        from hermes_cli.goals import GoalContract

        block = GoalContract(outcome="X", verification="Y").render_block()
        assert "Outcome: X" in block
        assert "Verification: Y" in block
        assert "Constraints" not in block


class TestGoalManagerContract:


    def test_set_contract_after_the_fact(self, hermes_home):
        from hermes_cli.goals import GoalManager, GoalContract

        mgr = GoalManager(session_id="c-after")
        mgr.set("ship it")
        assert not mgr.has_contract()
        mgr.set_contract(GoalContract(verification="x"))
        assert mgr.has_contract()
        # Survives reload.
        from hermes_cli.goals import GoalManager as GM2
        assert GM2(session_id="c-after").has_contract()

    def test_persistence_roundtrip(self, hermes_home):
        from hermes_cli.goals import GoalManager, GoalContract

        GoalManager(session_id="c-persist").set(
            "ship it", contract=GoalContract(outcome="O", verification="V")
        )
        reloaded = GoalManager(session_id="c-persist")
        assert reloaded.state.contract.outcome == "O"
        assert reloaded.state.contract.verification == "V"


class TestJudgeWithContract:
    def _fake_call_llm(self, captured, content='{"done": false, "reason": "more"}'):
        """judge_goal routes through call_llm (#35566) — capture its kwargs."""
        class _FakeMsg:
            pass
        _FakeMsg.content = content
        class _FakeChoice:
            message = _FakeMsg()
        class _FakeResp:
            choices = [_FakeChoice()]

        def _fake(**kwargs):
            captured.update(kwargs)
            return _FakeResp()
        return _fake

    def test_judge_uses_contract_template(self, hermes_home):
        from unittest.mock import patch
        from hermes_cli import goals
        from hermes_cli.goals import GoalContract

        captured = {}
        with patch("agent.auxiliary_client.call_llm",
                   side_effect=self._fake_call_llm(captured)):
            goals.judge_goal(
                "ship it", "I think it's done",
                contract=GoalContract(verification="pytest -q passes"),
            )
        user_msg = next(
            (m["content"] for m in (captured.get("messages") or []) if m["role"] == "user"), ""
        )
        assert "completion contract" in user_msg.lower()
        assert "pytest -q passes" in user_msg


class TestDraftContract:
    def test_draft_parses_json(self, hermes_home):
        from unittest.mock import patch
        from hermes_cli import goals

        class _FakeMsg:
            content = (
                '{"outcome": "auth on JWT", "verification": "auth suite green", '
                '"constraints": "no API change", "boundaries": "services/auth", '
                '"stop_when": "schema change needed"}'
            )
        class _FakeChoice:
            message = _FakeMsg()
        class _FakeResp:
            choices = [_FakeChoice()]
        with patch("agent.auxiliary_client.call_llm",
                   return_value=_FakeResp()):
            contract = goals.draft_contract("Migrate auth to JWT")
        assert contract is not None
        assert contract.outcome == "auth on JWT"
        assert contract.verification == "auth suite green"
        assert not contract.is_empty()


    def test_draft_returns_none_when_no_client(self, hermes_home):
        from unittest.mock import patch
        from hermes_cli import goals

        with patch("agent.auxiliary_client.call_llm",
                   side_effect=RuntimeError("No LLM provider configured")):
            assert goals.draft_contract("anything") is None


# ──────────────────────────────────────────────────────────────────────
# Compose: completion contract + wait barrier in one judge call
# ──────────────────────────────────────────────────────────────────────


class TestContractAndBackgroundCompose:
    """A contract goal blocked on a background process must surface BOTH
    the contract block and the background-process list to the judge, so it
    can return either done (evidence met) or wait (parked on the poller)."""

    def _capture_call_llm(self, captured, content='{"verdict": "wait", "wait_on_pid": 4242, "reason": "CI still running"}'):
        """judge_goal routes through call_llm (#35566) — capture its kwargs."""
        class _FakeMsg:
            pass
        _FakeMsg.content = content
        class _FakeChoice:
            message = _FakeMsg()
        class _FakeResp:
            choices = [_FakeChoice()]

        def _fake(**kwargs):
            captured.update(kwargs)
            return _FakeResp()
        return _fake

    def test_judge_prompt_carries_contract_and_background(self, hermes_home):
        from unittest.mock import patch
        from hermes_cli import goals
        from hermes_cli.goals import GoalContract

        captured = {}
        bg = [{
            "session_id": "ci-watch", "pid": 4242, "status": "running",
            "command": "wait_for_pr_green.sh 50501", "trigger": "exit",
        }]
        with patch("agent.auxiliary_client.call_llm",
                   side_effect=self._capture_call_llm(captured)):
            verdict, _reason, _parse_failed, wait_directive, _tf = goals.judge_goal(
                "ship the PR",
                "I pushed and started the CI watcher; waiting on it now.",
                contract=GoalContract(verification="PR CI goes green"),
                background_processes=bg,
            )
        user_msg = next(
            (m["content"] for m in (captured.get("messages") or []) if m["role"] == "user"), ""
        )
        # Both surfaces present in one prompt.
        assert "completion contract" in user_msg.lower()
        assert "PR CI goes green" in user_msg
        assert "Background processes" in user_msg
        assert "4242" in user_msg
        # The judge can return a wait verdict on a contract goal.
        assert verdict == "wait"
        assert wait_directive and wait_directive.get("pid") == 4242


class TestBlockedVerdict:
    """#100954: a genuinely unachievable goal must be refused, not completed."""

    def test_parse_judge_response_accepts_blocked(self):
        from hermes_cli.goals import _parse_judge_response

        verdict, reason, parse_failed, _wd = _parse_judge_response(
            '{"verdict": "blocked", "reason": "the repo was deleted"}'
        )
        assert verdict == "blocked"
        assert reason == "the repo was deleted"
        assert parse_failed is False

    def test_blocked_verdict_pauses_goal_instead_of_done(self, hermes_home):
        from unittest.mock import patch
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="blocked-sid")
        mgr.set("delete a repository that does not exist")
        with patch(
            "hermes_cli.goals.judge_goal",
            return_value=("blocked", "the repo does not exist", False, None, False),
        ):
            decision = mgr.evaluate_after_turn(
                "The repo cannot be deleted: it does not exist."
            )

        assert decision["verdict"] == "blocked"
        assert decision["status"] == "paused"
        assert decision["should_continue"] is False
        assert "blocked" in decision["message"].lower()
        assert mgr.state is not None
        assert mgr.state.status == "paused"
        assert "judge blocked" in (mgr.state.paused_reason or "").lower()


class TestBlockedGoalResumesOnUserInput:
    """A BLOCKED pause means "the next step needs user input". When the user then sends a
    real message, that message IS the input — the loop must pick the goal back up instead
    of leaving it paused until the user discovers ``/goal resume``."""

    @staticmethod
    def _blocked_goal(session_id: str):
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id=session_id)
        mgr.set("run the two agent sessions end to end")
        with patch(
            "hermes_cli.goals.judge_goal",
            return_value=("blocked", "the irreversible no-delete constraint would be breached", False, None, False),
        ):
            decision = mgr.evaluate_after_turn("I need your approval before copying the login.")
        assert decision["status"] == "paused"
        assert mgr.state.turns_used == 1
        return mgr

    def test_user_message_reactivates_a_blocked_goal(self, hermes_home):
        from hermes_cli.goals import GoalManager

        mgr = self._blocked_goal("blocked-resume-sid")

        assert mgr.resume_for_user_input() is True
        assert mgr.is_active()
        assert mgr.state.paused_reason is None
        # Continuing the same goal: the budget already spent stays spent.
        assert mgr.state.turns_used == 1
        assert GoalManager(session_id="blocked-resume-sid").is_active(), "reactivation must persist"

    def test_reactivated_goal_judges_the_users_turn_and_continues(self, hermes_home):
        mgr = self._blocked_goal("blocked-resume-judged")
        assert mgr.resume_for_user_input() is True
        with patch(
            "hermes_cli.goals.judge_goal",
            return_value=("continue", "approval given, sessions still pending", False, None, False),
        ):
            decision = mgr.evaluate_after_turn("Thanks — copying the login now and starting session A.")
        assert decision["should_continue"] is True
        assert decision["continuation_prompt"]
        assert mgr.state.turns_used == 2

    @pytest.mark.parametrize("pause_reason", ["user-paused", "user-interrupted (Ctrl+C)"])
    def test_explicit_user_pause_is_not_undone_by_a_message(self, hermes_home, pause_reason):
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id=f"explicit-pause-{pause_reason[:4]}")
        mgr.set("ship the release")
        mgr.pause(reason=pause_reason)

        assert mgr.resume_for_user_input() is False
        assert mgr.state.status == "paused"
        assert mgr.state.paused_reason == pause_reason

    def test_budget_pause_is_not_undone_by_a_message(self, hermes_home):
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="budget-pause-sid")
        mgr.set("ship the release", max_turns=1)
        with patch(
            "hermes_cli.goals.judge_goal",
            return_value=("continue", "still going", False, None, False),
        ):
            decision = mgr.evaluate_after_turn("did one thing")
        assert decision["status"] == "paused"

        assert mgr.resume_for_user_input() is False
        assert mgr.state.status == "paused"

    def test_no_goal_or_active_goal_is_a_noop(self, hermes_home):
        from hermes_cli.goals import GoalManager

        assert GoalManager(session_id="no-goal-sid").resume_for_user_input() is False
        mgr = GoalManager(session_id="active-goal-sid")
        mgr.set("ship the release")
        assert mgr.resume_for_user_input() is False
        assert mgr.is_active()


def test_goal_session_db_is_the_registry_shared_handle(hermes_home):
    """GoalManager must borrow the process-wide registry handle for ``state.db`` rather than
    minting a bare ``SessionDB()``: a second writer per profile carries its own token-writer
    thread and close-time checkpoint beside the gateway's handle (the #90837 corruption shape)."""
    from hermes_cli import goals
    import hermes_state_registry as registry

    db = goals._get_session_db()
    assert db is not None
    try:
        assert any(shared is db for shared in registry.live_shared_session_dbs())
        assert goals._get_session_db() is db
    finally:
        goals._DB_CACHE.clear()
        registry.release_or_close(db)


@pytest.mark.parametrize("mutation, expected_status", [("pause", "paused"), ("clear", "cleared")])
def test_judge_does_not_overwrite_concurrent_goal_mutation(hermes_home, mutation, expected_status):
    """A command issued while the judge is running remains authoritative."""
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager, load_goal

    session_id = f"judge-race-{mutation}"
    mgr = GoalManager(session_id=session_id)
    mgr.set("ship the release")

    def judge(*_args, **_kwargs):
        other = GoalManager(session_id=session_id)
        if mutation == "pause":
            other.pause()
        else:
            other.clear()
        return ("continue", "still going", False, None, False)

    with patch.object(goals, "judge_goal", side_effect=judge):
        decision = mgr.evaluate_after_turn("did one thing")

    persisted = load_goal(session_id)
    assert persisted is not None
    assert persisted.status == expected_status
    assert decision["should_continue"] is False
    assert decision["status"] == expected_status


def test_continuation_gap_normalization_is_uncapped(hermes_home):
    from hermes_cli.goals import GoalManager, normalize_goal_continuation_gap

    assert GoalManager(session_id="gap-default").min_continuation_gap_seconds == 0
    assert normalize_goal_continuation_gap(0) == 0
    assert normalize_goal_continuation_gap("bad") == 900
    assert normalize_goal_continuation_gap(-1) == 900
    assert normalize_goal_continuation_gap(86400) == 86400
