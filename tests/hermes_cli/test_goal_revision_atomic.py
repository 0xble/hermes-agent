"""Goal revisions cannot overwrite concurrent lifecycle or definition writes."""

import pytest

from hermes_cli import goals
from hermes_state import SessionDB


@pytest.mark.parametrize("timing", ["before", "evidence", "commit", "noop_commit"])
@pytest.mark.parametrize("change", ["pause", "clear", "replace", "revise", "pause_resume"])
def test_revision_preserves_the_durable_winner(tmp_path, monkeypatch, timing, change):
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    try:
        manager = goals.GoalManager("revision-race")
        original = manager.set("original task")
        # Legacy rows had no mutation token. Exact state equality must still matter.
        original.mutation_id = ""
        db.set_meta("goal:revision-race", original.to_json())
        before = original.to_json()
        winner = goals.GoalManager(manager.session_id)
        saved = []

        def revise():
            assert winner.revise(reason="Tighten proof", contract={"verification": "pass the suite"})["ok"]

        def pause_resume():
            winner.pause()
            winner.resume(reset_budget=False)

        def publish():
            {"pause": winner.pause, "clear": winner.clear,
             "replace": lambda: winner.set("replacement task"),
             "revise": revise, "pause_resume": pause_resume}[change]()
            saved.append(db.get_meta("goal:revision-race"))

        if timing == "before":
            publish()
        elif timing == "evidence":
            # Agents cannot change the objective (that is /goal replace), so the long read
            # inside a revision is the recorded-evidence lookup.
            def lookup(self, evidence, state):
                publish()
                return None
            monkeypatch.setattr(goals.GoalManager, "_recorded_evidence_error", lookup)
        else:
            execute = db._execute_write
            def interleave(write, *args, **kwargs):
                monkeypatch.setattr(db, "_execute_write", execute)
                publish()
                return execute(write, *args, **kwargs)
            monkeypatch.setattr(db, "_execute_write", interleave)
        if timing == "noop_commit":
            result = manager.revise(reason="Nothing changed", contract={})
        elif timing == "evidence":
            result = manager.revise(reason="Recorded audit", contract={"verification": "check the audit"},
                                    evidence="audit passed")
        else:
            result = manager.revise(reason="More proof", contract={"verification": "check the artifact"})
        assert result["ok"] is False
        assert result["error_code"] == "state_changed"
        assert saved and db.get_meta("goal:revision-race") == saved[-1]
        assert manager.state.to_json() == saved[-1]
        assert original.to_json() == before  # no staged edits leaked to callers
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["revise", "evaluate"])
@pytest.mark.parametrize("owner", ["same", "other"])
@pytest.mark.parametrize("change", ["pause", "clear", "replace"])
def test_revision_preserves_same_manager_controls_after_commit(tmp_path, monkeypatch, operation, owner, change):
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    try:
        manager = goals.GoalManager("revision-after-commit")
        manager.set("original task")
        execute = db._execute_write
        saved = []

        def interleave(write, *args, **kwargs):
            monkeypatch.setattr(db, "_execute_write", execute)
            result = execute(write, *args, **kwargs)
            # The revision is durable, but its staged state has not been published.
            control = manager if owner == "same" else goals.GoalManager(manager.session_id)
            {"pause": control.pause, "clear": control.clear,
             "replace": lambda: control.set("replacement task")}[change]()
            saved.append(db.get_meta("goal:revision-after-commit"))
            return result

        monkeypatch.setattr(db, "_execute_write", interleave)
        if operation == "revise":
            result = manager.revise(reason="More proof", contract={"verification": "check the artifact"})
            assert result["ok"] is True  # revision committed before the lifecycle command
        else:
            monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "more work", False, None, False))
            decision = manager.evaluate_after_turn("progress")
            assert decision["should_continue"] is False
            assert decision["continuation_prompt"] is None
        assert db.get_meta("goal:revision-after-commit") == saved[-1]
        if change == "clear":
            assert manager.state is None or manager.state.status == "cleared"
        else:
            assert manager.state.to_json() == saved[-1]
            manager.add_subgoal("verify the current task")
            after = goals.load_goal(manager.session_id)
            assert after.status == ("paused" if change == "pause" else "active")
            assert after.goal == ("replacement task" if change == "replace" else "original task")
    finally:
        db.close()


@pytest.mark.parametrize("failure", ["read", "write", "unavailable", "ephemeral"])
def test_revision_persistence_failure_is_not_ephemeral_success(tmp_path, monkeypatch, caplog, failure):
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    try:
        manager = goals.GoalManager("" if failure == "ephemeral" else "revision-failure")
        original = manager.set("original task")
        before = original.to_json()
        read = db.get_meta
        def fail(*args, **kwargs):
            raise OSError("disk unavailable")
        if failure == "read":
            monkeypatch.setattr(db, "get_meta", fail)
        elif failure == "write":
            monkeypatch.setattr(db, "_execute_write", fail)
        else:
            monkeypatch.setattr(goals, "_get_session_db", lambda: None)
        result = manager.revise(reason="More proof", contract={"verification": "check the artifact"})
        if failure == "ephemeral":
            assert result["ok"] is True
            assert manager.state.contract.verification == "check the artifact"
            monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("done", "verified", False, None, False))
            assert manager.evaluate_after_turn("verified")["status"] == "done"
            assert read("goal:") is None
        else:
            assert result["ok"] is False
            assert result["error_code"] == "persistence_failed"
            assert original.to_json() == before
            assert manager.state.to_json() == before
            assert read("goal:revision-failure") == before
            assert "could not read or commit" in caplog.text
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["stop_waiting", "clear_lifted_wait"])
def test_wait_controls_serialize_with_snapshot_publication(tmp_path, monkeypatch, operation):
    from contextlib import contextmanager

    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    try:
        manager = goals.GoalManager("wait-control-lock")
        manager.set("original task")
        monkeypatch.setattr(goals, "_pid_alive", lambda _pid: True)
        manager.wait_on(12345)
        entered = []

        @contextmanager
        def control_lock():
            entered.append(True)
            try:
                yield
            finally:
                entered.pop()

        manager._snapshot_lock = control_lock()
        if operation == "stop_waiting":
            save = manager._save

            def checked_save():
                assert entered, "wait control must serialize with snapshot publication"
                save()

            monkeypatch.setattr(manager, "_save", checked_save)
            assert manager.stop_waiting()
        else:
            target = "clear_goal_wait_if_since"
            write = getattr(goals, target)

            def checked_write(*args, **kwargs):
                assert entered, "wait control must serialize with snapshot publication"
                return write(*args, **kwargs)

            monkeypatch.setattr(goals, target, checked_write)
            assert manager.clear_lifted_wait(manager.state.waiting_since)
        assert not entered
        assert manager.state.to_json() == goals.load_goal(manager.session_id).to_json()
    finally:
        db.close()
