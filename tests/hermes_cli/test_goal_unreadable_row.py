"""A corrupt or unreadable goal row degrades to an inert goal instead of bricking the session.

Regression for the PR #551 review: once ``load_goal()`` refused to treat an unreadable row as
absent, ``GoalManager`` construction raised on every user turn and in the very commands that
recover the row. Writes must still refuse to overwrite it, except ``/goal clear``.
"""

import pytest

from hermes_cli import goals
from hermes_cli.goal_command import dispatch_goal_command
from hermes_state import SessionDB

SID = "corrupt"
KEY = f"goal:{SID}"


@pytest.fixture
def db(tmp_path, monkeypatch):
    store = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: store)
    yield store
    store.close()


def _dispatch(manager, command):
    return dispatch_goal_command(manager, command, authorize_gate=lambda: None)


def _corrupt(db):
    goals.GoalManager(SID).set("Ship the release")
    db.set_meta(KEY, "{corrupt")


def test_corrupt_row_is_inert_and_status_points_to_clear(db):
    _corrupt(db)
    manager = goals.GoalManager(SID)

    assert manager.unreadable and not manager.is_active() and not manager.has_goal()
    assert manager.next_continuation_prompt() is None
    assert manager.resume_for_user_input() is False
    status = _dispatch(manager, "status")
    assert not status.error
    assert "unreadable" in status.output and "/goal clear" in status.output
    with pytest.raises(goals.GoalPersistenceError, match="/goal clear"):
        manager.pause()
    assert db.get_meta(KEY) == "{corrupt"


def test_set_is_refused_until_clear_discards_the_corrupt_row(db):
    _corrupt(db)
    manager = goals.GoalManager(SID)

    refused = _dispatch(manager, "Start something else")
    assert refused.error and refused.prompt is None
    assert "/goal clear" in refused.output
    assert db.get_meta(KEY) == "{corrupt"

    cleared = _dispatch(manager, "clear")
    assert not cleared.error and cleared.output == "✓ Goal cleared."
    tombstone = goals.load_goal(SID)
    assert tombstone.status == "cleared"

    started = _dispatch(goals.GoalManager(SID), "Start something else")
    assert not started.error and started.prompt
    state = goals.load_goal(SID)
    assert (state.goal, state.status) == ("Start something else", "active")
    assert state.created_at > tombstone.created_at
