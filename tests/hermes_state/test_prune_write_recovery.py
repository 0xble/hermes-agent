"""Every capped prune write shrinks, scales its minimum unit, then defers it."""
from contextlib import closing
import json
import threading
import time

import pytest

from hermes_state import SessionDB
from hermes_state_mutation_retirement import RETIRED_PREFIX
from tests.hermes_state.test_prune_pass_bounds import _old_ended


def test_slow_retirement_batch_shrinks_and_finishes_without_a_pass_limit(tmp_path, monkeypatch):
    import hermes_state_mutation_retirement as retirement
    import hermes_state as state
    real_marker = retirement._mark_retired
    real_settle = state.settle_write_transaction
    holds = []
    delay = threading.Event()

    def marker(conn, key, **kwargs):
        delay.wait(0.015)
        return real_marker(conn, key, **kwargs)

    def settle(*args, **kwargs):
        started = time.monotonic()
        try:
            return real_settle(*args, **kwargs)
        finally:
            holds.append(time.monotonic() - started)

    with closing(SessionDB(tmp_path / "state.db")) as db:
        ids = [f"old-{i:02}" for i in range(25)]
        _old_ended(db, *ids)
        monkeypatch.setattr(retirement, "_mark_retired", marker)
        monkeypatch.setattr(state, "settle_write_transaction", settle)
        deleted = db.prune_sessions(exclude_active_write_guards=True)
        assert deleted == 25
        assert db._read_one("SELECT count(*) FROM sessions")[0] == 0
        assert max(holds) <= 2.0
        assert any(held > 0.25 for held in holds)


def test_minimum_retirement_defers_one_session_and_continues(tmp_path, monkeypatch, caplog):
    import hermes_state_mutation_retirement as retirement
    real_retire = retirement.retire_sessions

    def retire(conn, ids, **kwargs):
        if "stuck" in ids:
            conn.execute("WITH RECURSIVE work(n) AS (SELECT 1 UNION ALL "
                         "SELECT n+1 FROM work WHERE n < 50000000) SELECT sum(n) FROM work").fetchone()
        return real_retire(conn, ids, **kwargs)

    with closing(SessionDB(tmp_path / "state.db")) as db:
        _old_ended(db, "stuck", "small-a", "small-b")
        db.append_messages_batch("stuck", [{"role": "user", "content": "pending", "timestamp": 1.0}] * 20)
        monkeypatch.setattr(retirement, "retire_sessions", retire)
        db.prune_sessions(exclude_active_write_guards=True)
        assert db._read_one("SELECT count(*) FROM sessions WHERE id != 'stuck'")[0] == 0
        assert db._read_one("SELECT id FROM sessions WHERE id='stuck'") is not None
        cleanup = json.loads(db.get_meta(RETIRED_PREFIX + "stuck"))["cleanup"]
        assert cleanup["attempts"] == 1
        assert cleanup["last_expired_at"] > 0
        assert "writer ceiling" in caplog.text and "stuck" in caplog.text
        monkeypatch.setattr(retirement, "retire_sessions", real_retire)
        import hermes_state_deletion as deletion
        real_page, page_sizes, interrupted = deletion._delete_page, [], False
        def page(conn, sid, selected, key, marker):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise RuntimeError("stop after retirement resumes")
            page_sizes.append(len(selected))
            return real_page(conn, sid, selected, key, marker)
        monkeypatch.setattr(deletion, "_delete_page", page)
        with pytest.raises(RuntimeError, match="stop after retirement resumes"):
            db.prune_sessions()
        cleanup = json.loads(db.get_meta(RETIRED_PREFIX + "stuck"))["cleanup"]
        assert not {"retirement_pending", "attempts", "last_expired_at"} & cleanup.keys()
        db.prune_sessions()
        assert max(page_sizes) > 1
        assert db._read_one("SELECT count(*) FROM sessions")[0] == 0
        assert "cleanup" not in json.loads(db.get_meta(RETIRED_PREFIX + "stuck"))


@pytest.mark.parametrize("interleave", ["none", "pin", "reactivate", "guard"])
def test_fallback_fence_rechecks_eligibility_after_the_rolled_back_stage(tmp_path, monkeypatch, interleave):
    """A foreground write between the rolled-back stage and the fallback fence keeps the session."""
    import os
    import sqlite3
    import hermes_state_deletion as deletion
    import hermes_state_mutation_retirement as retirement
    from hermes_state_prune_budget import without_prune_budget
    real_retire, real_stage_delete = retirement.retire_sessions, deletion.stage_delete

    def burn(conn):
        conn.execute("WITH RECURSIVE work(n) AS (SELECT 1 UNION ALL "
                     "SELECT n+1 FROM work WHERE n < 50000000) SELECT sum(n) FROM work").fetchone()

    def retire(conn, ids, **kwargs):
        if "victim" in ids:
            burn(conn)
        return real_retire(conn, ids, **kwargs)

    def stage_delete(conn, ids, sessions_dir=None):
        if "victim" in ids:
            burn(conn)
        return real_stage_delete(conn, ids, sessions_dir)

    with closing(SessionDB(tmp_path / "state.db")) as db:
        _old_ended(db, "victim")
        db.append_messages_batch("victim", [{"role": "user", "content": "keep", "timestamp": 1.0}] * 5)
        messages = db._read_one("SELECT count(*) FROM messages WHERE session_id='victim'")[0]
        real_write, interleaved = db._execute_write, []

        def foreground_write():
            if interleave in ("pin", "reactivate"):
                with closing(sqlite3.connect(tmp_path / "state.db")) as other, other:
                    other.execute("UPDATE sessions SET pinned=1 WHERE id='victim'" if interleave == "pin"
                                  else "UPDATE sessions SET ended_at=NULL, end_reason=NULL WHERE id='victim'")
            elif interleave == "guard":
                with without_prune_budget():
                    assert db.try_acquire_compression_lock("victim", f"pid={os.getpid()}:cmp=1", ttl_seconds=300.0)

        def execute_write(fn, *args, **kwargs):
            # The stage transaction has rolled back by the time its fallback is attempted.
            if getattr(fn, "__qualname__", "").endswith("defer_retirement.<locals>.record_attempt"):
                interleaved.append(fn.__qualname__)
                foreground_write()
            return real_write(fn, *args, **kwargs)

        monkeypatch.setattr(retirement, "retire_sessions", retire)
        monkeypatch.setattr(deletion, "stage_delete", stage_delete)
        monkeypatch.setattr(db, "_execute_write", execute_write)
        db.prune_sessions(exclude_active_write_guards=True)
        assert interleaved, "the deferred retirement stage must roll back before the fallback fence"
        marker = db.get_meta(RETIRED_PREFIX + "victim")
        if interleave == "none":
            assert json.loads(marker)["cleanup"]["retirement_pending"] is True
            return
        assert marker is None
        monkeypatch.undo()
        # The next pass must not resume an irreversible cleanup of the kept session.
        db.prune_sessions(exclude_active_write_guards=True)
        assert db.get_meta(RETIRED_PREFIX + "victim") is None
        assert db._read_one("SELECT count(*) FROM messages WHERE session_id='victim'")[0] == messages
        row = db._read_one("SELECT pinned, ended_at FROM sessions WHERE id='victim'")
        assert row is not None
        if interleave == "pin":
            assert row[0] == 1
