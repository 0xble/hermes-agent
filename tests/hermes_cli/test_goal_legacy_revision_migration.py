"""Upgrade history-backed legacy requirements into the additive-only current contract."""

import copy
import json

import pytest

from hermes_cli import goals
from hermes_state import SessionDB


OLD_CHECK = "Run the security audit and every integration test."


@pytest.fixture
def db(tmp_path, monkeypatch):
    store = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: store)
    yield store
    store.close()


def _seed(db, *, revisions=None, contract=None, subgoals=None, status="active"):
    state = goals.GoalState(
        goal="Ship safely", status=status, max_turns=7, turns_used=2,
        contract=goals.GoalContract.from_dict(contract or {"verification": "Say done"}),
        subgoals=subgoals or [],
        revisions=copy.deepcopy(revisions if revisions is not None else [{
            "actor": "agent", "reason": "Use a shorter verification", "user_quote": "",
            "before": {"verification": OLD_CHECK}, "after": {"verification": "Say done"},
        }]),
        mutation_id="before-upgrade",
    )
    db.set_meta("goal:legacy", state.to_json())
    return state


@pytest.mark.parametrize("status", ["active", "paused", "done", "cleared"])
def test_load_restores_reviewers_security_audit_reproduction_in_current_contract(db, monkeypatch, status):
    _seed(db, status=status)

    state = goals.load_goal("legacy")
    assert state is not None

    assert goals.contract_items("verification", state.contract.verification) == ["Say done", OLD_CHECK]
    assert OLD_CHECK in goals.GoalManager._current_continuation_prompt(state)
    assert state.status == status
    assert (state.max_turns, state.turns_used) == (7, 2)
    stored = json.loads(db.get_meta("goal:legacy"))
    assert stored["contract"]["verification"] == state.contract.verification
    assert stored["revisions"][0]["legacy_binding_migrated"] is True
    assert state.mutation_id != "before-upgrade"
    assert state.revisions[0]["before"]["verification"] == OLD_CHECK
    prompts = []

    def capture(_call, _system, prompt, _timeout):
        prompts.append(prompt)
        return '{"verdict":"continue","reason":"audit still required"}'

    monkeypatch.setattr(goals, "_call_goal_judge_llm", capture)
    goals.judge_goal(state.goal, "Say done", contract=state.contract, timeout=1)
    assert OLD_CHECK in prompts[0]
    assert "earlier verification" not in prompts[0]


def test_load_restores_every_field_the_base_history_renderer_kept_binding(db):
    before = {
        "goal": "Original objective.", "outcome": "Original deliverable.",
        "verification": "Original check.", "constraints": "Never delete records.",
        "boundaries": "Stay in staging.", "stop_when": "Stop if secrets are missing.",
        "subgoals": ["Keep the full smoke test. Both services must pass.", "Retained criterion."],
    }
    after = {**dict.fromkeys(before, ""), "goal": "Ship safely", "subgoals": ["Retained criterion."]}
    _seed(db, contract={key: "Current item." for key in goals._CONTRACT_FIELDS},
          subgoals=["Current criterion.", "Retained criterion."], revisions=[{
              "actor": "agent", "reason": "Narrow the bar", "before": before, "after": after,
              # A stored quote was only judge-interpreted authority in the base design.
              "user_quote": "Narrow the contract", "user_message": "Narrow the contract",
          }])

    state = goals.load_goal("legacy")
    assert state is not None

    assert state.goal == "Ship safely"
    assert goals.contract_items("outcome", state.contract.outcome) == [
        "Current item.", "Original objective.", "Original deliverable.",
    ]
    for key in ("verification", "constraints", "boundaries", "stop_when"):
        assert goals.contract_items(key, getattr(state.contract, key)) == ["Current item.", before[key]]
    assert state.subgoals == [
        "Current criterion.", "Retained criterion.", "Keep the full smoke test. Both services must pass.",
    ]


def test_restored_items_dedupe_exact_normalized_identity_and_keep_current_then_history_order(db):
    _seed(db, contract={"verification": "Keep this.\nAudit passes.\nAudit passes."}, revisions=[{
        "before": {"verification": "Audit   passes.\nAudit passes!\naudit passes.\nSecond check."},
        "after": {"verification": "Keep this."},
    }, {
        "before": {"verification": "Second check.\nThird check."},
        "after": {"verification": "Keep this."},
    }])

    state = goals.load_goal("legacy")
    assert state is not None

    assert goals.contract_items("verification", state.contract.verification) == [
        "Keep this.", "Audit passes.", "Audit passes!", "audit passes.", "Second check.", "Third check.",
    ]


def test_load_migration_is_persisted_once_and_explicit_removal_does_not_resurrect_items(db, monkeypatch):
    _seed(db)
    first = goals.load_goal("legacy")
    assert first is not None
    persisted = db.get_meta("goal:legacy")

    def unexpected_write(_txn):
        pytest.fail("a second load must not write or migrate again")

    with monkeypatch.context() as patch:
        patch.setattr(db, "_execute_write", unexpected_write)
        second = goals.load_goal("legacy")
    assert second is not None
    assert second.to_json() == first.to_json()
    assert db.get_meta("goal:legacy") == persisted

    manager = goals.GoalManager("legacy")
    assert manager.remove_contract_item("verification", 2) == OLD_CHECK
    removed = db.get_meta("goal:legacy")
    reloaded = goals.load_goal("legacy")
    assert reloaded is not None
    assert goals.contract_items("verification", reloaded.contract.verification) == ["Say done"]
    assert db.get_meta("goal:legacy") == removed


def test_rows_written_by_new_agent_and_explicit_command_paths_are_untouched(db):
    manager = goals.GoalManager("modern")
    manager.set("Ship safely", contract=goals.GoalContract(verification=OLD_CHECK))
    assert manager.revise(reason="Add another check", contract={"verification": OLD_CHECK + "\nSay done"})["ok"]
    assert manager.remove_contract_item("verification", 1) == OLD_CHECK
    stored = db.get_meta("goal:modern")

    loaded = goals.load_goal("modern")
    assert loaded is not None

    assert goals.contract_items("verification", loaded.contract.verification) == ["Say done"]
    assert [row["authority"] for row in loaded.revisions] == ["agent_addition", "explicit_command"]
    assert all("legacy_binding_migrated" not in row for row in loaded.revisions)
    assert db.get_meta("goal:modern") == stored


def test_new_authority_rows_in_a_mixed_history_never_restore_their_before_values(db):
    _seed(db, revisions=[{
        "before": {"verification": OLD_CHECK}, "after": {"verification": "Say done"},
    }, {
        "authority": "explicit_command", "before": {"verification": "Retired via slash command."},
        "after": {"verification": "Say done"},
    }, {
        "authority": "evidence", "before": {"verification": "Modern evidence row."},
        "after": {"verification": "Modern evidence row.\nSay done"},
    }])
    original = json.loads(db.get_meta("goal:legacy"))["revisions"]

    loaded = goals.load_goal("legacy")
    assert loaded is not None

    assert goals.contract_items("verification", loaded.contract.verification) == ["Say done", OLD_CHECK]
    assert loaded.revisions[1:] == original[1:]


@pytest.mark.parametrize("replacement", ["new_contract", "cleared", "deleted"])
def test_load_migration_rereads_the_live_row_in_its_write_transaction(db, monkeypatch, replacement):
    _seed(db)
    get_meta = db.get_meta
    expected = []

    def racing_read(key):
        raw = get_meta(key)
        if not expected:
            if replacement == "deleted":
                db._execute_write(lambda conn: conn.execute("DELETE FROM state_meta WHERE key = ?", (key,)))
                expected.append(None)
            else:
                state = goals.GoalState.from_json(raw)
                state.status = "cleared" if replacement == "cleared" else "active"
                state.contract.verification = "User command's current check."
                state.revisions = [{"authority": "explicit_command", "before": {}, "after": {}}]
                state.mutation_id = "concurrent-command"
                db.set_meta(key, state.to_json())
                expected.append(state.to_json())
        return raw

    monkeypatch.setattr(db, "get_meta", racing_read)

    loaded = goals.load_goal("legacy")

    assert (loaded.to_json() if loaded else None) == expected[0]
    assert get_meta("goal:legacy") == expected[0]


def test_migration_write_failure_leaves_legacy_row_retryable_without_partial_persistence(db, monkeypatch):
    _seed(db)
    raw = db.get_meta("goal:legacy")

    def failed_write(_txn):
        raise RuntimeError("storage is temporarily unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(db, "_execute_write", failed_write)
        loaded = goals.load_goal("legacy")
        assert loaded is not None
        assert getattr(loaded, "_legacy_migration_pending") is True
        manager = goals.GoalManager("legacy")
        with pytest.raises(goals.GoalPersistenceError, match="refusing to overwrite"):
            manager.set("a replacement goal")
    assert db.get_meta("goal:legacy") == raw
    loaded = goals.load_goal("legacy")
    assert loaded is not None
    assert OLD_CHECK in loaded.contract.verification
    assert loaded.revisions[0]["legacy_binding_migrated"] is True


def test_legacy_migration_preserves_no_progress_ladder(db):
    legacy = _seed(db)
    legacy.consecutive_no_progress = 2
    legacy.backoff_level = 2
    db.set_meta("goal:legacy", legacy.to_json())

    migrated = goals.load_goal("legacy")

    assert migrated.consecutive_no_progress == migrated.backoff_level == 2
    assert OLD_CHECK in migrated.contract.verification
    stored = json.loads(db.get_meta("goal:legacy"))
    assert stored["consecutive_no_progress"] == stored["backoff_level"] == 2


@pytest.mark.parametrize("stale_manager", [False, True])
def test_goal_command_after_failed_legacy_migration_does_not_destroy_existing_goal(db, monkeypatch, stale_manager):
    from hermes_cli.goal_command import dispatch_goal_command

    manager = goals.GoalManager("legacy") if stale_manager else None
    _seed(db)
    before = db.get_meta("goal:legacy")
    write = db._execute_write

    def migration_fails_once(txn):
        # Loading uses the migration write; the command's old set/save path can still write. This
        # models a transient migration failure, not an outage that happens to block all writes.
        monkeypatch.setattr(db, "_execute_write", write)
        raise RuntimeError("migration write failed")

    monkeypatch.setattr(db, "_execute_write", migration_fails_once)
    if manager is None:
        manager = goals.GoalManager("legacy")
        # Repeat the failure for the fresh read in set().
        monkeypatch.setattr(db, "_execute_write", migration_fails_once)
    result = dispatch_goal_command(manager, "new unrelated objective", authorize_gate=lambda: None)

    assert result.error is True
    assert result.prompt is None
    assert "refusing to overwrite" in result.output
    assert db.get_meta("goal:legacy") == before
    assert manager.state.goal == "Ship safely"
    assert OLD_CHECK in manager.state.contract.verification

    # The next real mutation re-reads, migrates, and persists the existing goal rather than
    # persisting the pending marker or discarding the old binding requirement.
    manager.pause(reason="user-paused")
    saved = goals.GoalState.from_json(db.get_meta("goal:legacy"))
    assert saved.goal == "Ship safely"
    assert saved.status == "paused"
    assert OLD_CHECK in saved.contract.verification
    assert saved.revisions[0]["legacy_binding_migrated"] is True

    retry = dispatch_goal_command(manager, "new unrelated objective", authorize_gate=lambda: None)
    assert not retry.error
    assert retry.prompt is None and retry.kickoff is False
    assert manager.state.goal == "new unrelated objective"


def test_cached_manager_bare_replace_retries_after_migration_saves(db, monkeypatch):
    """A cached CLI manager holding a pending marker retries the migration instead of refusing forever."""
    from hermes_cli.goal_command import dispatch_goal_command

    _seed(db)
    write = db._execute_write

    def migration_fails_once(txn):
        monkeypatch.setattr(db, "_execute_write", write)
        raise RuntimeError("migration write failed")

    monkeypatch.setattr(db, "_execute_write", migration_fails_once)
    manager = goals.GoalManager("legacy")
    assert getattr(manager.state, "_legacy_migration_pending", False)

    result = dispatch_goal_command(manager, "new unrelated objective", authorize_gate=lambda: None)

    assert not result.error, result.output
    assert result.prompt is None and result.kickoff is False
    saved = goals.GoalState.from_json(db.get_meta("goal:legacy"))
    assert saved.goal == "new unrelated objective"
    assert OLD_CHECK in saved.contract.verification
    assert not getattr(manager.state, "_legacy_migration_pending", False)


@pytest.mark.parametrize("failure", ["read", "parse"])
def test_set_refuses_to_overwrite_an_existing_goal_it_cannot_read_cleanly(db, monkeypatch, failure):
    manager = goals.GoalManager("legacy")
    _seed(db)
    if failure == "parse":
        db.set_meta("goal:legacy", "{corrupt goal row")
    before = db.get_meta("goal:legacy")
    with monkeypatch.context() as patch:
        if failure == "read":
            patch.setattr(db, "get_meta", lambda key: (_ for _ in ()).throw(RuntimeError("read failed")))
        with pytest.raises(goals.GoalPersistenceError, match="could not be"):
            manager.set("new objective")
    assert db.get_meta("goal:legacy") == before


def test_cursor_goal_load_migrates_inside_existing_writer_transaction(db):
    """Session-control writers cannot re-enter SessionDB's non-reentrant writer lock."""
    _seed(db)

    def apply(cursor):
        manager = goals.GoalManager("legacy", cursor=cursor)
        result = manager.replace(reason="session-control", goal="new objective", actor="user")
        assert result["ok"], result
        return manager.state

    saved = db._execute_write(apply)
    assert saved.goal == "new objective"
    assert goals.load_goal("legacy").goal == "new objective"


@pytest.mark.parametrize("route", ["command", "session_control"])
def test_replace_migrates_an_unmigrated_legacy_row_in_the_same_commit(db, monkeypatch, route):
    """Explicit replacement never commits an unmigrated legacy row, so its old requirements stay
    on the replaced goal now rather than reappearing on the next load."""
    from hermes_cli.goal_command import dispatch_goal_command

    _seed(db)
    write = db._execute_write

    def migration_fails_once(txn):
        monkeypatch.setattr(db, "_execute_write", write)
        raise RuntimeError("migration write failed")

    monkeypatch.setattr(db, "_execute_write", migration_fails_once)
    manager = goals.GoalManager("legacy")
    assert getattr(manager.state, "_legacy_migration_pending", False)

    if route == "command":
        result = dispatch_goal_command(manager, "replace new objective", authorize_gate=lambda: None)
        assert not result.error, result.output
    else:
        result = manager.replace(reason="approved", goal="new objective",
                                 contract={"verification": "Fresh check"}, actor="user")
        assert result["ok"], result

    saved = goals.GoalState.from_json(db.get_meta("goal:legacy"))
    assert saved.goal == "new objective"
    assert saved.revisions[0]["legacy_binding_migrated"] is True
    assert not getattr(manager.state, "_legacy_migration_pending", False)
    assert manager.state.to_json() == saved.to_json()
    # Reloading is a no-op: nothing from the old history is re-applied after the replacement.
    assert goals.load_goal("legacy").to_json() == saved.to_json()
