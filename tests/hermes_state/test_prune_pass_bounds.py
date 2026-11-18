"""A retention prune pass: one per store at a time, and only inside the deletion window when bound to it.

Regression for the 2026-11-07 daytime prune: a client timed out, asked again while the gateway
was still running the shielded first pass, and two passes on one store held the write lock in
turn for five hours.
"""
import threading
import time
from contextlib import closing

import pytest

import hermes_cli.maintenance_window
import hermes_state_maintenance
from hermes_state import SessionDB
from hermes_state_prune_pacing import DeletionWindowClosedError, PruneInProgressError


def _old_ended(db, *ids):
    at = time.time() - 200 * 86400
    for sid in ids:
        db.create_session(sid, source="cron")
        db.append_message(sid, "user", sid, timestamp=at)
        db.end_session(sid, "done")
    db._execute_write(lambda conn: conn.execute(
        f"UPDATE sessions SET started_at=?, ended_at=?, last_activity_at=? WHERE id IN ({','.join('?' * len(ids))})",
        (at, at, at, *ids)))


def test_a_second_pass_on_the_same_store_is_refused_while_the_first_runs(tmp_path, monkeypatch):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        _old_ended(db, "a", "b")
        entered, release = threading.Event(), threading.Event()
        real_finish = hermes_state_maintenance.finish_pending_deletes

        def held_first_pass(db_, **kwargs):
            if not entered.is_set():
                entered.set()
                assert release.wait(10)
            return real_finish(db_, **kwargs)
        monkeypatch.setattr(hermes_state_maintenance, "finish_pending_deletes", held_first_pass)
        first = {}
        runner = threading.Thread(target=lambda: first.update(n=db.prune_sessions(older_than_days=90)))
        runner.start()
        assert entered.wait(10)

        with pytest.raises(PruneInProgressError):
            db.prune_sessions(older_than_days=90)
        release.set()
        runner.join(10)

        assert first["n"] == 2
        # The slot is released: the next pass runs (nothing left to delete).
        assert db.prune_sessions(older_than_days=90) == 0


def test_a_window_bound_pass_refuses_to_start_and_stops_when_the_window_closes(tmp_path, monkeypatch):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        ids = [f"s{n:02d}" for n in range(30)]  # more than one 25-session transaction
        _old_ended(db, *ids)
        monkeypatch.setattr(hermes_cli.maintenance_window, "deletion_window_open", lambda: False)
        with pytest.raises(DeletionWindowClosedError):
            db.prune_sessions(older_than_days=90, within_deletion_window=True)
        assert all(db.get_session(sid) is not None for sid in ids)

        checks = iter([True, True])  # the start gate and the first batch, then the window closes
        monkeypatch.setattr(hermes_cli.maintenance_window, "deletion_window_open", lambda: next(checks, False))
        deleted = db.prune_sessions(older_than_days=90, within_deletion_window=True)

        assert deleted == 25
        assert sum(db.get_session(sid) is None for sid in ids) == 25


def test_a_local_reset_conversation_is_pruned_whole_even_one_session_per_transaction(tmp_path, monkeypatch):
    """Its segments sweep only together, so a shrunken batch must still carry the whole conversation."""
    from tests.fakes.runtime_owner import begin_runtime_epoch
    from tests.hermes_state.test_target_advance_fence import _local_session, _mutate
    real_pacer = hermes_state_maintenance.PrunePacer
    monkeypatch.setattr(hermes_state_maintenance, "PrunePacer", lambda max_size: real_pacer(max_size=1))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with closing(SessionDB(tmp_path / "state.db")) as db:
        epoch = begin_runtime_epoch(db, instance_id="owner")
        owner = _local_session(db, epoch, tmp_path)
        child = _mutate(db, epoch, owner, "reset")["target_session_id"]
        db.append_message(child, "user", "turn")
        db.end_session(child, "done")
        at = time.time() - 200 * 86400
        db._execute_write(lambda conn: (
            conn.execute("UPDATE sessions SET started_at=?, ended_at=?, last_activity_at=? WHERE id IN (?,?)",
                         (at, at, at, owner, child)),
            conn.execute("UPDATE messages SET timestamp=? WHERE session_id IN (?,?)", (at, owner, child))))

        assert db.prune_sessions(older_than_days=90) == 2
        assert db.get_session(owner) is None and db.get_session(child) is None
