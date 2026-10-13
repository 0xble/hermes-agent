"""Goal revisions cannot overwrite concurrent lifecycle or definition writes."""

import pytest

from hermes_cli import goals
from hermes_state import SessionDB


@pytest.mark.parametrize("timing", ["before", "quote", "commit", "noop_commit"])
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
        elif timing == "quote":
            def messages(*args):
                publish()
                return ["Work on the corrected task instead"]
            monkeypatch.setattr(goals, "user_messages_since", messages)
        else:
            execute = db._execute_write
            def interleave(write, *args, **kwargs):
                monkeypatch.setattr(db, "_execute_write", execute)
                publish()
                return execute(write, *args, **kwargs)
            monkeypatch.setattr(db, "_execute_write", interleave)
        if timing == "noop_commit":
            result = manager.revise(reason="Nothing changed", contract={})
        elif timing == "quote":
            result = manager.revise(reason="New task", goal="corrected task",
                                    user_quote="Work on the corrected task instead")
        else:
            result = manager.revise(reason="More proof", contract={"verification": "check the artifact"})
        assert result["ok"] is False
        assert result["error_code"] == "state_changed"
        assert saved and db.get_meta("goal:revision-race") == saved[-1]
        assert manager.state.to_json() == saved[-1]
        assert original.to_json() == before  # no staged edits leaked to callers
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
