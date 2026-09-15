"""Notice bookkeeping cannot overwrite newer durable goal controls."""
from pathlib import Path

import pytest

from hermes_cli import goals
from hermes_cli.goals_evaluation import state_token


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert Path(goals.__file__).resolve().parents[1] == Path(__file__).resolve().parents[2]
    goals._DB_CACHE.clear()
    mgr = goals.GoalManager("notice-fence")
    mgr.set("original objective")
    yield mgr
    goals._DB_CACHE.clear()


@pytest.mark.parametrize("control", ["pause", "clear", "duplicate", "local_revision"])
def test_notice_claim_respects_concurrent_control(manager, monkeypatch, control):
    db = goals._get_session_db()
    original_write = db._execute_write
    decision = {"transition": "waiting", "reason": "child"}
    entered = False

    def interleaved_write(operation, **kwargs):
        nonlocal entered
        if not entered:
            entered = True
            other = goals.GoalManager(manager.session_id)
            if control == "duplicate":
                assert other.claim_transition_notice(decision)
            elif control == "local_revision":
                # A local stop still revokes work when its revision DB write fails.
                key = goals._goal_control_cache_key(manager.session_id)
                goals._MODEL_GOAL_CONTROL_REVISIONS[key] = 1
            else:
                getattr(other, control)()
                saved = goals.load_goal(manager.session_id)
                assert saved.user_stopped and saved.status != "active"
        return original_write(operation, **kwargs)

    monkeypatch.setattr(db, "_execute_write", interleaved_write)
    assert manager.claim_transition_notice(decision) is False
    saved = goals.load_goal(manager.session_id)
    if control in {"pause", "clear"}:
        assert saved.status == {"pause": "paused", "clear": "cleared"}[control]
        assert saved.user_stopped is True
        assert saved.last_notice_key is None


@pytest.mark.parametrize("change", ["state", "revision", "session", "write_failure"])
def test_uncommittable_notice_cannot_change_state(manager, monkeypatch, change):
    db = goals._get_session_db()
    decision = {"transition": "done", "_goal_authority": {
        "session_id": manager.session_id, "revision": 0,
        "state_token": state_token(db.get_meta(goals._meta_key(manager.session_id))),
    }}
    if change == "state":
        goals.GoalManager(manager.session_id).pause()
    elif change == "revision":
        goals.advance_goal_control_revision(manager.session_id)
    elif change == "write_failure":
        def unavailable(*args, **kwargs):
            raise OSError("storage unavailable")
        monkeypatch.setattr(db, "_execute_write", unavailable)
    else:
        decision["_goal_authority"]["session_id"] = "other-session"
    before = db.get_meta(goals._meta_key(manager.session_id))
    assert manager.claim_transition_notice(decision) is False
    assert db.get_meta(goals._meta_key(manager.session_id)) == before

