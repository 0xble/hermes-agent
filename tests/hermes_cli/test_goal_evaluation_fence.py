"""Goal evaluation must not overwrite controls received while gates or judging run."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import goals
from hermes_cli.goal_outcomes import prepare_goal_turn


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    goals._DB_CACHE.clear()
    mgr = goals.GoalManager("evaluation-fence", default_max_turns=10)
    mgr.set("original objective")
    yield mgr
    goals._DB_CACHE.clear()


def _thread(call):
    result, errors = [], []
    def run():
        try:
            result.append(call())
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, result, errors


@pytest.mark.parametrize(("verdict", "control"), [
    ("done", "pause"), ("continue", "clear"), ("wait", "replace"),
    ("blocked", "edit"), ("transport", "pause"), ("parse", "pause"),
    ("budget", "pause"),
])
def test_slow_judge_cannot_overwrite_new_control(manager, monkeypatch, verdict, control):
    entered, release = threading.Event(), threading.Event()
    if verdict == "transport":
        manager.state.consecutive_transport_failures = goals.DEFAULT_MAX_CONSECUTIVE_TRANSPORT_FAILURES - 1
    if verdict == "parse":
        manager.state.consecutive_parse_failures = goals.DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES - 1
    if verdict == "budget":
        manager.state.max_turns = 1
    manager._save()
    def judge(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return ("continue" if verdict in {"transport", "parse", "budget"} else verdict,
                "old evaluation", verdict == "parse", {"seconds": 5} if verdict == "wait" else None,
                verdict == "transport")
    monkeypatch.setattr(goals, "judge_goal", judge)
    worker, results, errors = _thread(lambda: manager.evaluate_after_turn("old response"))
    try:
        assert entered.wait(5)
        current = goals.GoalManager(manager.session_id)
        if control == "pause":
            current.pause("user pause")
        elif control == "clear":
            current.clear()
        elif control == "replace":
            current.set("new objective")
        else:
            current.edit("revised objective", contract=goals.GoalContract())
        expected = goals.load_goal(manager.session_id).to_json()
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not errors
    assert goals.load_goal(manager.session_id).to_json() == expected
    assert results == [{}]


@pytest.mark.parametrize(("passed", "max_retries"), [(True, 2), (False, 2), (False, 0)])
def test_gate_changes_are_private_until_evaluation_commit(manager, monkeypatch, passed, max_retries):
    manager.add_gate("true")
    manager.state.gates[0].max_retries = max_retries
    manager._save()
    before = goals.load_goal(manager.session_id).to_json()
    entered, release = threading.Event(), threading.Event()
    def gate(*args, **kwargs):
        if not passed:
            entered.set()
            assert release.wait(10)
        return passed, 0 if passed else 1, "gate result"
    def judge(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return "done", "old completion", False, None, False
    monkeypatch.setattr(goals, "run_gate", gate)
    monkeypatch.setattr(goals, "workspace_fingerprint", lambda **kw: "fingerprint")
    monkeypatch.setattr(goals, "judge_goal", judge)
    worker, results, errors = _thread(lambda: manager.evaluate_after_turn("old response"))
    try:
        assert entered.wait(5)
        intermediate = goals.load_goal(manager.session_id).to_json()
        goals.GoalManager(manager.session_id).pause("user pause during gate")
        expected = goals.load_goal(manager.session_id).to_json()
    finally:
        release.set()
        worker.join(10)
    assert not errors and not worker.is_alive()
    assert intermediate == before
    assert goals.load_goal(manager.session_id).to_json() == expected
    assert results == [{}]


def test_other_process_control_defeats_pending_evaluation(manager, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def judge(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return "continue", "old response", False, None, False
    monkeypatch.setattr(goals, "judge_goal", judge)
    worker, results, errors = _thread(lambda: manager.evaluate_after_turn("old response"))
    try:
        assert entered.wait(5)
        subprocess.run([sys.executable, "-c",
            "from hermes_cli.goals import GoalManager; m=GoalManager('evaluation-fence'); "
            "assert m.pause('other process').user_stopped"],
            env={**os.environ, "HERMES_STATE_DB_GUARD_BYPASS": "1"},
            check=True, capture_output=True, timeout=10)
        expected = goals.load_goal(manager.session_id).to_json()
    finally:
        release.set()
        worker.join(10)
    assert not errors and not worker.is_alive()
    assert goals.load_goal(manager.session_id).to_json() == expected
    assert results == [{}]


def test_local_unsaved_gate_edit_preserved_when_storage_unchanged(manager, monkeypatch):
    manager.add_gate("true")
    manager.state.gates[0].max_retries = 7
    monkeypatch.setattr(goals, "run_gate", lambda *a, **kw: (False, 1, "failed"))
    monkeypatch.setattr(goals, "workspace_fingerprint", lambda **kw: "fingerprint")
    decision = manager.evaluate_after_turn("working")
    assert decision["should_continue"]
    assert goals.load_goal(manager.session_id).gates[0].max_retries == 7


def test_unavailable_storage_cannot_authorize_automatic_evaluation(manager, monkeypatch):
    before = goals.load_goal(manager.session_id).to_json()
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("done", "done", False, None, False))
    with monkeypatch.context() as m:
        m.setattr(goals, "_get_session_db", lambda: None)
        assert manager.evaluate_after_turn("done") == {}
        assert manager.unexpected_stop("failed turn") == {}
    assert goals.load_goal(manager.session_id).to_json() == before


def test_classifier_cannot_borrow_replacement_goal_authority(manager, monkeypatch):
    manager.state.max_turns = 1
    manager._save()
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("continue", "more work", False, None, False))
    def classify(self, decision, text, **kwargs):
        goals.GoalManager(self.session_id).set("new objective")
        return "old goal stopped"
    monkeypatch.setattr(goals.GoalManager, "prepare_goal_outcome", classify)
    monkeypatch.setattr(goals, "count_active_delegations", lambda *a: 0)
    monkeypatch.setattr(goals, "gather_background_processes", lambda **kw: [])
    result = {"final_response": "old response", "completed": True}
    prepare_goal_turn(manager, SimpleNamespace(_interrupt_requested=False), result)
    assert result["_goal_decision"] == {}
    assert result["final_response"] == "old response"
    assert goals.load_goal(manager.session_id).goal == "new objective"


def test_cancellation_after_gate_save_discards_the_entire_draft(manager, monkeypatch):
    manager.add_gate("true")
    baseline = goals.load_goal(manager.session_id).to_json()
    current = [True]
    monkeypatch.setattr(goals, "run_gate", lambda *a, **kw: (True, 0, "passed"))
    monkeypatch.setattr(goals, "workspace_fingerprint", lambda **kw: "fingerprint")
    def judge(*args, **kwargs):
        assert goals.load_goal(manager.session_id).to_json() == baseline
        current[0] = False
        return "done", "done", False, None, False
    monkeypatch.setattr(goals, "judge_goal", judge)
    assert manager.evaluate_after_turn("done", is_current=lambda: current[0]) == {}
    assert goals.load_goal(manager.session_id).to_json() == baseline
    assert manager.state.turns_used == 0


def test_unexpected_stop_wait_cleanup_cannot_overwrite_control(manager, monkeypatch):
    manager.wait_on(12345, reason="pending")
    entered, release = threading.Event(), threading.Event()
    def pid_alive(pid):
        entered.set()
        assert release.wait(10)
        return False
    monkeypatch.setattr(goals, "_pid_alive", pid_alive)
    worker, results, errors = _thread(lambda: manager.unexpected_stop("old failed turn"))
    try:
        assert entered.wait(5)
        goals.GoalManager(manager.session_id).set("replacement")
        expected = goals.load_goal(manager.session_id).to_json()
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not errors
    assert results == [{}]
    assert goals.load_goal(manager.session_id).to_json() == expected


def test_competing_evaluations_commit_only_once(manager, monkeypatch):
    barrier = threading.Barrier(3)
    release = threading.Event()
    def judge(*args, **kwargs):
        barrier.wait(timeout=10)
        assert release.wait(10)
        return "continue", "more work", False, None, False
    monkeypatch.setattr(goals, "judge_goal", judge)
    other = goals.GoalManager(manager.session_id)
    workers = [_thread(lambda mgr=mgr: mgr.evaluate_after_turn("response")) for mgr in [manager, other]]
    try:
        barrier.wait(timeout=10)
    finally:
        release.set()
        for worker, _, _ in workers:
            worker.join(10)
    assert all(not worker.is_alive() and not errors for worker, _, errors in workers)
    assert sum(bool(results[0]) for _, results, _ in workers) == 1
    assert goals.load_goal(manager.session_id).turns_used == 1


@pytest.mark.parametrize("new_control", [False, True])
def test_dirty_local_state_requires_unchanged_accepted_baseline(manager, monkeypatch, new_control):
    before = goals.load_goal(manager.session_id).to_json()
    manager.state.subgoals.append("unsaved local criterion")
    with monkeypatch.context() as m:
        m.setattr(goals, "save_goal", lambda *a, **kw: False)
        manager._save()
    # A high timestamp must never authorize overwriting a changed persisted row.
    manager._state.updated_at += 10000
    assert goals.load_goal(manager.session_id).to_json() == before
    if new_control:
        goals.GoalManager(manager.session_id).pause("newer stored control")
        expected = goals.load_goal(manager.session_id).to_json()
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("continue", "more work", False, None, False))
    result = manager.evaluate_after_turn("working")
    if new_control:
        assert result == {}
        assert goals.load_goal(manager.session_id).to_json() == expected
        assert manager.state.user_stopped
    else:
        assert result["should_continue"]
        assert goals.load_goal(manager.session_id).subgoals == ["unsaved local criterion"]


def test_goal_load_does_not_pair_old_state_with_new_baseline(manager, monkeypatch):
    db = goals._get_session_db()
    original = db.get_meta
    armed = [True]
    def racing_read(key):
        value = original(key)
        if armed[0] and key == goals._meta_key(manager.session_id):
            armed[0] = False
            goals.GoalManager(manager.session_id).pause("control during load")
        return value
    monkeypatch.setattr(db, "get_meta", racing_read)
    stale = goals.GoalManager(manager.session_id)
    expected = goals.load_goal(manager.session_id).to_json()
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("done", "old", False, None, False))
    assert stale.evaluate_after_turn("done") == {}
    assert goals.load_goal(manager.session_id).to_json() == expected


def test_revision_cache_and_locks_are_profile_scoped(manager, tmp_path, monkeypatch):
    import hermes_state
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    # Restore the normal call-time DB resolver for this explicit two-profile test.
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    with monkeypatch.context() as profile:
        profile.setenv("HERMES_HOME", str(home_a))
        goals.GoalManager(manager.session_id).set("profile a")
        revision_a = goals.advance_goal_control_revision(manager.session_id)
        lock_a = goals._goal_control_lock(manager.session_id)
    with monkeypatch.context() as profile:
        profile.setenv("HERMES_HOME", str(home_b))
        second = goals.GoalManager(manager.session_id)
        second.set("profile b")
        assert goals.get_goal_control_revision(manager.session_id) == 0
        assert goals._goal_control_lock(manager.session_id) is not lock_a
        monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("continue", "working", False, None, False))
        assert second.evaluate_after_turn("working", expected_revision=0)["should_continue"]
    with monkeypatch.context() as profile:
        profile.setenv("HERMES_HOME", str(home_a))
        assert goals.get_goal_control_revision(manager.session_id) == revision_a
        assert goals.load_goal(manager.session_id).turns_used == 0


def test_revision_only_change_invalidates_pending_judgment(manager, monkeypatch):
    baseline = goals.load_goal(manager.session_id).to_json()
    def judge(*args, **kwargs):
        goals.advance_goal_control_revision(manager.session_id)
        return "done", "obsolete", False, None, False
    monkeypatch.setattr(goals, "judge_goal", judge)
    assert manager.evaluate_after_turn("done") == {}
    assert goals.load_goal(manager.session_id).to_json() == baseline


def test_failed_commit_never_exposes_draft_as_live_state(manager, monkeypatch):
    baseline = goals.load_goal(manager.session_id).to_json()
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("done", "done", False, None, False))
    def unavailable(*args, **kwargs):
        raise OSError("storage unavailable")
    monkeypatch.setattr(goals._get_session_db(), "compare_and_set_meta", unavailable)
    assert manager.evaluate_after_turn("done") == {}
    assert goals.load_goal(manager.session_id).to_json() == baseline
    assert manager.state.status == "active" and manager.state.turns_used == 0


def test_dependency_loss_pause_is_fenced(manager, monkeypatch):
    manager._state.waiting_on_delegation = "deleg_missing"
    manager._save()
    expected = []
    def missing(_dependency):
        goals.GoalManager(manager.session_id).pause("new user pause")
        expected.append(goals.load_goal(manager.session_id).to_json())
        return "missing", None
    monkeypatch.setattr(goals, "_delegation_dependency", missing)
    assert manager.evaluate_after_turn("old child response") == {}
    assert goals.load_goal(manager.session_id).to_json() == expected[0]


def test_unpersisted_control_revision_still_invalidates_local_work(manager, monkeypatch):
    before = goals.load_goal(manager.session_id).to_json()
    db = goals._get_session_db()
    original = db.set_meta
    def reject_revision(key, value, **kwargs):
        if key == goals._goal_control_revision_key(manager.session_id):
            raise OSError("revision write failed")
        return original(key, value, **kwargs)
    monkeypatch.setattr(db, "set_meta", reject_revision)
    def judge(*args, **kwargs):
        goals.advance_goal_control_revision(manager.session_id)
        return "done", "obsolete", False, None, False
    monkeypatch.setattr(goals, "judge_goal", judge)
    assert manager.evaluate_after_turn("done") == {}
    assert goals.load_goal(manager.session_id).to_json() == before
    # A fresh evaluator in this process must not forget the failed durable invalidation.
    assert manager.evaluate_after_turn("done") == {}
