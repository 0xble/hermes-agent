"""A nightly auto-prune pass the deletion window cuts off keeps its progress and resumes."""

import time

import pytest

import hermes_cli.maintenance_window
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _stale_ended_session(db: SessionDB, session_id: str) -> None:
    stale = time.time() - 60 * 86400
    db.create_session(session_id=session_id, source="cli")
    db.end_session(session_id, "cli_close")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?, last_activity_at = ? WHERE id = ?",
        (stale, stale, stale, session_id),
    )
    db._conn.commit()


def test_pass_cut_off_by_window_records_partial_progress_and_next_window_finishes(db, monkeypatch):
    window = {"open": True}
    monkeypatch.setattr(hermes_cli.maintenance_window, "deletion_window_open", lambda: window["open"])
    _stale_ended_session(db, "old-1")
    real_prune = db.prune_sessions

    def prune_until_window_closes(*args, **kwargs):
        pruned = real_prune(*args, **kwargs)
        window["open"] = False  # the window closes while the pass is still running
        return pruned

    monkeypatch.setattr(db, "prune_sessions", prune_until_window_closes)

    first = db.maybe_auto_prune_and_vacuum(retention_days=30, min_interval_hours=24, vacuum=False)

    assert first["deadline_reached"] is True
    assert first["pruned"] == 1
    assert db.get_session("old-1") is None  # committed work survives the cut-off
    assert db.get_meta("last_auto_prune_partial") is not None
    assert db.get_meta("last_auto_prune") is None  # throttle unspent: the next window resumes

    monkeypatch.setattr(db, "prune_sessions", real_prune)
    window["open"] = True
    _stale_ended_session(db, "old-2")

    second = db.maybe_auto_prune_and_vacuum(retention_days=30, min_interval_hours=24, vacuum=False)

    assert second["skipped"] is False
    assert second["pruned"] == 1
    assert "deadline_reached" not in second
    assert db.get_meta("last_auto_prune") is not None

    third = db.maybe_auto_prune_and_vacuum(retention_days=30, min_interval_hours=24, vacuum=False)

    assert third["skipped"] is True  # a completed pass spends the 24 h throttle


def test_pass_stopped_by_gateway_shutdown_is_partial_and_resumes(db, monkeypatch):
    import threading

    from hermes_state_prune_budget import prune_cancel

    monkeypatch.setattr(hermes_cli.maintenance_window, "deletion_window_open", lambda: True)
    _stale_ended_session(db, "old-1")
    stop = threading.Event()
    stop.set()  # the gateway is stopping before the pass deletes anything

    with prune_cancel(stop):
        result = db.maybe_auto_prune_and_vacuum(retention_days=30, min_interval_hours=24, vacuum=False)

    assert result["deadline_reached"] is True
    assert result["pruned"] == 0
    assert db.get_session("old-1") is not None
    assert db.get_meta("last_auto_prune") is None  # the next window resumes it
    assert db.get_meta("last_auto_prune_partial") is not None


def test_shutdown_during_the_sweep_does_not_spend_the_throttle(db, monkeypatch):
    import threading

    from hermes_state_prune_budget import prune_cancel

    monkeypatch.setattr(hermes_cli.maintenance_window, "deletion_window_open", lambda: True)
    stop = threading.Event()
    real_sweep = db.sweep_orphaned_sessions

    def sweep_then_shutdown(*args, **kwargs):
        closed = real_sweep(*args, **kwargs)
        stop.set()
        return closed

    monkeypatch.setattr(db, "sweep_orphaned_sessions", sweep_then_shutdown)

    with prune_cancel(stop):
        result = db.maybe_auto_prune_and_vacuum(retention_days=30, min_interval_hours=24, vacuum=True)

    assert result["deadline_reached"] is True
    assert result["vacuumed"] is False
    assert db.get_meta("last_auto_prune") is None
    assert db.get_meta("last_auto_prune_partial") is not None
