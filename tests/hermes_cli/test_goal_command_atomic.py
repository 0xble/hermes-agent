"""User goal commands apply against the CURRENT durable row, never a stale manager snapshot.

The gateway loads the command manager and dispatches the command in separate executor hops, so
the post-turn evaluator can commit in between. A command must keep the evaluator's lifecycle
fields (status, pause reason, turns used) and apply only its own contract/objective edit.
"""

import pytest

from hermes_cli import goals
from hermes_cli.goal_command import dispatch_goal_command
from hermes_state import SessionDB

SID = "session"


@pytest.fixture
def db(tmp_path, monkeypatch):
    store = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: store)
    monkeypatch.setattr(goals, "collect_goal_evidence", lambda *a, **k: [])
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "more work", False, None, False))
    yield store
    store.close()


def _seed():
    manager = goals.GoalManager(SID)
    manager.set("Ship the release", max_turns=1, contract=goals.GoalContract.from_dict({
        "constraints": "No downtime.\nNo secrets.",
        "verification": "Security audit passes.\nSmoke test passes.",
    }))
    manager.add_subgoal("Migrate Willow")
    manager.add_subgoal("Update release notes")
    return manager


def _exhaust_budget():
    """Another manager (the post-turn evaluator) spends the final permitted turn."""
    evaluator = goals.GoalManager(SID)
    decision = evaluator.evaluate_after_turn("did some work")
    assert decision["status"] == "paused" and decision["should_continue"] is False
    stored = goals.load_goal(SID)
    assert stored.status == "paused" and stored.turns_used == 1
    return stored


def _dispatch(manager, command):
    return dispatch_goal_command(manager, command, authorize_gate=lambda: None)


COMMANDS = [
    ("replace Ship polished result", lambda s: s.goal == "Ship polished result"),
    ("remove check 1", lambda s: goals.contract_items("verification", s.contract.verification) == ["Smoke test passes."]),
    ("remove constraint 1", lambda s: goals.contract_items("constraints", s.contract.constraints) == ["No secrets."]),
    ("remove subgoal 1", lambda s: goals.contract_items("subgoals", s.subgoals) == ["Update release notes"]),
]


@pytest.mark.parametrize("command,edited", COMMANDS, ids=[c for c, _ in COMMANDS])
def test_stale_command_manager_keeps_concurrent_budget_pause(db, command, edited):
    """Reviewer reproduction: command manager loaded at active 0/1, evaluator persists paused 1/1."""
    _seed()
    stale = goals.GoalManager(SID)
    assert stale.state.status == "active" and stale.state.turns_used == 0
    paused = _exhaust_budget()

    result = _dispatch(stale, command)

    assert not result.error, result.output
    stored = goals.load_goal(SID)
    assert stored.status == "paused"
    assert stored.turns_used == 1 and stored.max_turns == 1
    assert stored.paused_reason == paused.paused_reason
    assert edited(stored)
    assert result.prompt is None   # an exhausted, paused goal never schedules a continuation
    assert stale.state.to_json() == stored.to_json()   # caller refreshed from the written row


@pytest.mark.parametrize("command,edited", COMMANDS, ids=[c for c, _ in COMMANDS])
def test_evaluator_commit_inside_command_write_is_preserved(db, monkeypatch, command, edited):
    """The evaluator commits at the command's own write boundary; both changes survive."""
    _seed()
    command_manager = goals.GoalManager(SID)
    real_write = db._execute_write
    raced = []

    def racing_write(callback, *args, **kwargs):
        if not raced:
            raced.append(True)
            monkeypatch.setattr(db, "_execute_write", real_write)
            _exhaust_budget()
        return real_write(callback, *args, **kwargs)

    monkeypatch.setattr(db, "_execute_write", racing_write)
    result = _dispatch(command_manager, command)

    assert raced and not result.error, result.output
    stored = goals.load_goal(SID)
    assert (stored.status, stored.turns_used) == ("paused", 1)
    assert stored.paused_reason.startswith("turn budget exhausted")
    assert edited(stored)
    assert result.prompt is None


def test_replace_of_exhausted_paused_goal_stays_paused_without_prompt(db):
    manager = _seed()
    _exhaust_budget()
    manager = goals.GoalManager(SID)

    result = _dispatch(manager, "replace Ship polished result")

    stored = goals.load_goal(SID)
    assert not result.error
    assert (stored.goal, stored.status, stored.turns_used) == ("Ship polished result", "paused", 1)
    assert result.prompt is None
    assert stored.revisions[-1]["kind"] == "replace"


def test_active_replace_still_continues_with_current_state(db):
    _seed()
    stale = goals.GoalManager(SID)
    # Another surface added a subgoal after this manager loaded: replace must not drop it.
    goals.GoalManager(SID).add_subgoal("Announce in channel")

    result = _dispatch(stale, "replace Ship polished result")

    stored = goals.load_goal(SID)
    assert stored.status == "active" and stored.turns_used == 0
    assert "Announce in channel" in goals.contract_items("subgoals", stored.subgoals)
    assert result.prompt and "Ship polished result" in result.prompt


@pytest.mark.parametrize("mutate", [
    lambda m: m.add_subgoal("extra"),
    lambda m: m.add_gate("true"),
    lambda m: m.set_contract(goals.GoalContract.from_dict({"verification": "Done."})),
    lambda m: m.revise(reason="add a check", contract={"verification": "Security audit passes.\nSmoke test passes.\nLint passes."}),
], ids=["add_subgoal", "add_gate", "set_contract", "agent_revise"])
def test_other_manager_mutations_never_refund_turns(db, mutate):
    _seed()
    stale = goals.GoalManager(SID)
    _exhaust_budget()

    mutate(stale)

    stored = goals.load_goal(SID)
    assert (stored.status, stored.turns_used) == ("paused", 1)
    assert stale.state.to_json() == stored.to_json()


def test_remove_rejected_against_live_row_refreshes_caller(db):
    _seed()
    stale = goals.GoalManager(SID)
    goals.GoalManager(SID).remove_subgoal(2)   # concurrent removal shrinks the live list

    result = _dispatch(stale, "remove subgoal 2")

    assert result.error and "index out of range" in result.output
    assert goals.contract_items("subgoals", stale.state.subgoals) == ["Migrate Willow"]
    assert goals.contract_items("subgoals", goals.load_goal(SID).subgoals) == ["Migrate Willow"]


def test_stale_clear_after_concurrent_clear_does_not_resurrect(db):
    _seed()
    stale = goals.GoalManager(SID)
    goals.GoalManager(SID).clear()
    stale.pause()
    assert goals.load_goal(SID).status == "cleared"


@pytest.mark.parametrize("command", ["remove check 1", "replace Ship polished result", "pause"])
@pytest.mark.parametrize("failure", ["before_transaction", "after_mutation"])
def test_failed_command_write_never_changes_state_or_schedules_work(db, monkeypatch, command, failure):
    manager = _seed()
    local_state = manager.state
    local_before = local_state.to_json()
    # The command's local snapshot may already be stale when persistence fails.
    durable_before = _exhaust_budget().to_json()
    real_write = db._execute_write
    prompts = []
    monkeypatch.setattr(manager, "next_continuation_prompt", lambda: prompts.append(True))

    def failed_write(callback, *args, **kwargs):
        def fail_after_mutation(conn):
            callback(conn)
            raise OSError("disk unavailable")
        if failure == "after_mutation":
            return real_write(fail_after_mutation, *args, **kwargs)
        raise OSError("disk unavailable")

    monkeypatch.setattr(db, "_execute_write", failed_write)
    result = _dispatch(manager, command)

    assert result.error, result.output
    assert "goal change was not saved; try again" in result.output
    assert result.clear_pending is None and result.prompt is None and not result.kickoff
    assert prompts == []
    assert manager.state is local_state and manager.state.to_json() == local_before
    assert goals.load_goal(SID).to_json() == durable_before


@pytest.mark.parametrize("operation", ["add_subgoal", "remove_subgoal", "clear_subgoals", "resume_for_user_input", "mark_done"])
def test_failed_lifecycle_write_never_reports_success_or_changes_state(db, monkeypatch, operation):
    manager = _seed()
    manager.pause("judge blocked: waiting for user input")
    manager.state.last_verdict = "blocked"
    manager._save()
    before = manager.state.to_json()
    real_write = db._execute_write

    def failed_write(callback, *args, **kwargs):
        def fail_after_mutation(conn):
            callback(conn)
            raise OSError("disk unavailable")
        return real_write(fail_after_mutation, *args, **kwargs)

    monkeypatch.setattr(db, "_execute_write", failed_write)
    actions = {
        "add_subgoal": lambda: manager.add_subgoal("Check rollback"),
        "remove_subgoal": lambda: manager.remove_subgoal(1),
        "clear_subgoals": manager.clear_subgoals,
        "resume_for_user_input": manager.resume_for_user_input,
        "mark_done": lambda: manager.mark_done("verified"),
    }
    if operation == "resume_for_user_input":
        # Revive runs inside every admitted user turn: it reports "not resumed" instead of raising.
        assert actions[operation]() is False
    else:
        with pytest.raises(RuntimeError, match="goal change was not saved; try again"):
            actions[operation]()
    assert manager.state.to_json() == before
    assert goals.load_goal(SID).to_json() == before


def test_resume_resets_no_progress_ladder(db):
    manager = _seed()
    live = goals.load_goal(SID)
    live.consecutive_no_progress = 2
    goals.save_goal(SID, live)

    manager.resume()

    assert goals.load_goal(SID).consecutive_no_progress == 0
    assert manager.state.consecutive_no_progress == 0
