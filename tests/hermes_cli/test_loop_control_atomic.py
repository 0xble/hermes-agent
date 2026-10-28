"""Cached controls must edit the live loop row, never restore an older lifecycle.

User controls apply to the stored row; agent revisions commit only against their exact snapshot.
"""

from typing import Any

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from hermes_cli import loops

    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(loops, "_get_session_db", lambda: db)
    monkeypatch.setattr("hermes_cli.goals._get_session_db", lambda: db)
    for sid in ("controls", "failure"):
        db.create_session(session_id=sid, source="cli", model="test")
        db.append_message(sid, "user", QUOTE)
    yield db
    db.close()


QUOTE = "Please replace the loop with the deployment readiness check"


def control(manager, operation) -> Any:
    if operation == "save":
        return manager._save()
    if operation == "revise":
        return manager.revise(reason="poll less often", interval_seconds=600)
    if operation == "replace":
        return manager.replace(prompt="deployment readiness", interval_seconds=600,
                               reason="task changed", user_quote=QUOTE, user_messages=[QUOTE])
    return getattr(manager, operation)()


@pytest.mark.parametrize("newer,operation,expected_status", [
    ("pause", "save", "paused"),
    ("clear", "pause", "cleared"),
    ("clear", "resume", "cleared"),
    ("clear", "revise", "cleared"),
    ("clear", "replace", "cleared"),
    ("pause", "revise", "paused"),
    ("pause", "resume", "active"),
    ("pause", "replace", "paused"),
    ("tick", "revise", "active"),
    ("replace", "save", "active"),
    ("replace", "revise", "active"),
    ("replace", "pause", "paused"),
    ("replace", "resume", "active"),
    ("replace", "clear", "cleared"),
])
@pytest.mark.parametrize("cursor_owned", [False, True])
def test_stale_controls_preserve_the_current_lifecycle(store, newer, operation, expected_status, cursor_owned):
    from hermes_cli.loops import LoopManager, load_loop

    stale = LoopManager("controls")
    stale.set("watch", interval_seconds=300, times=8, route={"chat_id": "42"})
    current = LoopManager("controls")
    if newer == "tick":
        assert current.fire_tick(run_generation=7)
    elif newer == "replace":
        assert current.replace(prompt="deployment readiness", interval_seconds=300,
                               times=5, until="ready", route={"chat_id": "43"},
                               reason="task changed", user_quote=QUOTE, user_messages=[QUOTE])["ok"]
    else:
        assert control(current, newer)
    before = load_loop("controls")
    assert before is not None

    def apply(cursor):
        stale._cursor = cursor
        return control(stale, operation)

    result = store._execute_write(apply) if cursor_owned else control(stale, operation)
    after = load_loop("controls")
    assert after is not None

    assert after.status == expected_status
    assert after.route == before.route
    if operation != "replace":
        assert after.created_at == before.created_at
        assert after.times == before.times
        assert after.until == before.until
    if operation in {"revise", "replace"}:
        # Agent edits commit only against the exact snapshot they read: a stale one is
        # rejected and refreshed, never applied over the newer lifecycle or in-flight tick.
        assert result["ok"] is False and result["error_code"] == "state_changed"
        assert after.to_json() == before.to_json()
    elif newer == "clear":
        assert not result
        assert after.to_json() == before.to_json()
    elif operation == "save":
        assert after.to_json() == before.to_json()
    else:
        assert result
        assert after.ticks_fired == before.ticks_fired
        if newer == "replace":
            assert after.prompt == "deployment readiness"
    if operation == "clear":
        assert stale.state is None
    else:
        assert stale.state is not None
        assert stale.state.to_json() == after.to_json()


@pytest.mark.parametrize("operation", ["pause", "resume", "clear", "revise", "replace"])
@pytest.mark.parametrize("cursor_owned", [False, True])
def test_control_write_failure_does_not_apply_locally(store, monkeypatch, operation, cursor_owned):
    from hermes_cli.loops import LoopManager, load_loop

    manager = LoopManager("failure")
    manager.set("watch", interval_seconds=300)
    initial = load_loop("failure")
    assert initial is not None
    before = initial.to_json()

    def fail(*args, **kwargs):
        raise RuntimeError("write failed")

    if cursor_owned:
        def transaction(cursor):
            manager._cursor = cursor
            monkeypatch.setattr(store, "set_meta", fail)
            with pytest.raises(RuntimeError, match="write failed"):
                control(manager, operation)
        store._execute_write(transaction)
    else:
        monkeypatch.setattr(store, "_execute_write", fail)
        result = control(manager, operation)
        assert not result or result["ok"] is False
    assert manager.state is not None
    assert manager.state.to_json() == before
    stored = load_loop("failure")
    assert stored is not None and stored.to_json() == before


def test_cursor_owned_stale_claim_cannot_undo_a_pause(store):
    """A cursor owner holds the write transaction; the stale manager still CAS-checks the row."""
    from hermes_cli.loops import LoopManager, load_loop

    LoopManager("cursor-claim").set("watch", interval_seconds=60)

    def transaction(cursor):
        stale = LoopManager("cursor-claim", cursor=cursor)
        LoopManager("cursor-claim", cursor=cursor).pause()
        return stale.fire_tick()

    assert store._execute_write(transaction) is None
    after = load_loop("cursor-claim")
    assert after.status == "paused" and not after.awaiting_response and after.ticks_fired == 0


def test_cursor_owned_stale_settle_cannot_clear_a_successor_tick(store):
    from hermes_cli.loops import LoopManager, load_loop

    manager = LoopManager("cursor-settle")
    manager.set("watch", interval_seconds=60)
    assert manager.fire_tick()
    tick = (manager.state.created_at, manager.state.ticks_fired)

    def transaction(cursor):
        stale = LoopManager("cursor-settle", cursor=cursor)
        LoopManager("cursor-settle", cursor=cursor).pause()
        return stale.complete_tick("done", tick=tick)

    store._execute_write(transaction)
    after = load_loop("cursor-settle")
    assert after.status == "paused"


def test_cursor_owned_revision_and_settlement_commit_inside_the_owner_transaction(store):
    """The exact-snapshot CAS runs through the owner's cursor, never a second (deadlocking) lock."""
    from hermes_cli.loops import LoopManager, load_loop

    manager = LoopManager("cursor-commit")
    manager.set("watch", interval_seconds=300)
    assert manager.fire_tick()

    def transaction(cursor):
        owned = LoopManager("cursor-commit", cursor=cursor)
        assert owned.revise(reason="poll less often", interval_seconds=600)["ok"]
        return owned.complete_tick("still watching")

    decision = store._execute_write(transaction)
    after = load_loop("cursor-commit")
    assert decision["status"] == "active" and decision["stopped"] is False
    assert after.interval_seconds == 600 and after.version == 2
    assert not after.awaiting_response and after.ticks_fired == 1
