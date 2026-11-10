"""Every capped prune write shrinks, scales its minimum unit, then defers it."""
from contextlib import closing
import json
import threading
import time

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
        if deleted < 25:
            deleted += db.prune_sessions(exclude_active_write_guards=True)
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
        monkeypatch.setattr(retirement, "retire_sessions", retire)
        db.prune_sessions(exclude_active_write_guards=True)
        assert db._read_one("SELECT count(*) FROM sessions WHERE id != 'stuck'")[0] == 0
        assert db._read_one("SELECT id FROM sessions WHERE id='stuck'") is not None
        cleanup = json.loads(db.get_meta(RETIRED_PREFIX + "stuck"))["cleanup"]
        assert cleanup["attempts"] == 1
        assert cleanup["last_expired_at"] > 0
        assert "writer ceiling" in caplog.text and "stuck" in caplog.text
        monkeypatch.setattr(retirement, "retire_sessions", real_retire)
        db.prune_sessions()
        assert db._read_one("SELECT count(*) FROM sessions")[0] == 0
        assert "cleanup" not in json.loads(db.get_meta(RETIRED_PREFIX + "stuck"))
