"""Explicit deletion throughput and retention-only pacing/maintenance contracts."""
from contextlib import closing
import logging

import pytest

from hermes_state import SessionDB
from tests.module_clock import install_module_clock


@pytest.mark.parametrize("operation", ["single", "bulk", "runtime", "prune"])
def test_three_thousand_message_delete_uses_operation_pacing(tmp_path, monkeypatch, operation):
    import hermes_state_deletion as deletion
    import hermes_state_media as media
    import hermes_state_prune_budget as budgets
    from tests.hermes_state.test_prune_pass_bounds import _old_ended

    with closing(SessionDB(tmp_path / "state.db")) as db:
        _old_ended(db, "large")
        db.append_messages_batch("large", [
            {"role": "user", "content": "transcript", "timestamp": 1.0} for _ in range(2999)])
        clock = install_module_clock(monkeypatch, deletion)
        pauses, pages, steps, reclaimed = [], [], [], []
        monkeypatch.setattr(clock, "sleep", pauses.append, raising=False)
        monkeypatch.setattr(budgets.PruneBudget, "pause", lambda self, seconds: pauses.append(seconds))
        real_page, real_delete, real_retire = deletion._page_ids, deletion._delete_page, media.retire_spillover

        def page(*args, **kwargs):
            selected, size = real_page(*args, **kwargs)
            pages.append((kwargs["max_rows"], len(selected)))
            return selected, size

        def retire(*args, **kwargs):
            if kwargs.get("message_ids"):
                steps.append(len(kwargs["message_ids"]))
            return real_retire(*args, **kwargs)

        def delete_page(conn, sid, selected, key, marker):
            reclaimed.append(len(selected))
            return real_delete(conn, sid, selected, key, marker)

        monkeypatch.setattr(deletion, "_page_ids", page)
        monkeypatch.setattr(deletion, "_delete_page", delete_page)
        monkeypatch.setattr(media, "retire_spillover", retire)
        if operation == "single":
            assert db.delete_session("large")
        elif operation == "bulk":
            assert db.delete_sessions(["large"]) == 1
        elif operation == "runtime":
            from hermes_state_runtime import mutate_runtime_session
            from tests.fakes.runtime_owner import begin_runtime_epoch
            epoch = begin_runtime_epoch(db, instance_id="delete-test")
            snap = db.get_session("large")
            result = mutate_runtime_session(db, epoch=epoch, principal_id="human", session_id="large",
                request_id="delete", operation="delete", payload={},
                expected_revision=snap["runtime_revision"], expected_generation=snap["runtime_generation"])
            assert result["deleted_ids"] == ["large"]
        else:
            assert db.prune_sessions() == 1
        retention = operation == "prune"
        limits = {limit for limit, _ in pages}
        if retention:
            # Retention starts at the explicit 16-row cap, then PrunePacer may
            # shrink it when a writer transaction exceeds its target hold time.
            assert 16 in limits
            assert limits <= {1, 2, 4, 8, 16}
        else:
            assert limits == {64}
        if retention:
            # The 16-row value is a cap, not a promise that every transaction
            # reaches it: PrunePacer shrinks slow writer transactions and may
            # later grow them again.
            assert sum(count > 0 for count in reclaimed) >= 188
        else:
            assert sum(count > 0 for count in reclaimed) == 47
        assert max(steps) == (1 if retention else 8)
        assert pauses
        if retention:
            # Writer fairness pauses track measured hold time, with a 50 ms
            # floor and 2 s ceiling. Slow pages therefore produce longer
            # pauses than the floor.
            assert 0.05 in pauses
            assert all(0.05 <= pause <= 2.0 for pause in pauses)
        else:
            assert set(pauses) == {0.01}
        assert db._read_one("SELECT count(*) FROM messages")[0] == 0
        assert db.get_session("large") is None


@pytest.mark.parametrize("retention,expected", [(False, 26), (True, 6)])
def test_delete_page_byte_limit_follows_budget(monkeypatch, retention, expected):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from hermes_state_deletion import _page_ids
    from hermes_state_prune_budget import PruneBudget, prune_budget

    db = SimpleNamespace(_read_all=lambda sql, params: [(i, 10000) for i in range(params[1])])
    with prune_budget(PruneBudget()) if retention else nullcontext():
        selected, size = _page_ids(db, "large")
    assert len(selected) == expected
    assert size == expected * 10000


def test_unbounded_foreign_writer_retries_warn_every_thirty_seconds(tmp_path, monkeypatch, caplog):
    import hermes_state_prune_budget as budgets
    import hermes_state_lockowners as owners

    clock = install_module_clock(monkeypatch, budgets, now=0)
    attempts, warnings = [], []
    monkeypatch.setattr(clock, "sleep", lambda seconds: setattr(clock, "now", clock.now + 10), raising=False)
    monkeypatch.setattr(owners, "state_db_write_lock_holders", lambda path: ["pid=123 holds WAL write lock"])
    budget = budgets.PruneBudget()
    budget.db_path = tmp_path / "state.db"

    def attempt():
        attempts.append(clock.now)
        warnings.append(len(caplog.records))
        if clock.now < 100:
            raise budgets.PruneWriteContended("foreign writer")
        return "finished"

    with caplog.at_level(logging.WARNING, logger=budgets.__name__), budgets.prune_budget(budget):
        assert budgets.run_prune_write(attempt) == "finished"
    assert attempts == list(range(0, 101, 10))
    assert warnings == [0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 3]
    assert len(caplog.records) == 3
    assert all("pid=123" in record.message and "retry" in record.message for record in caplog.records)


@pytest.mark.parametrize("exhausted", [False, True])
def test_prune_merges_fts_once_at_end_only_with_remaining_budget(tmp_path, monkeypatch, exhausted):
    import hermes_state_prune_budget as budgets
    from tests.hermes_state.test_prune_pass_bounds import _old_ended

    with closing(SessionDB(tmp_path / "state.db")) as db:
        _old_ended(db, "old")
        clock = install_module_clock(monkeypatch, budgets, now=0)
        real_pass, real_merge = db._prune_sessions_pass, db._merge_fts_incrementally
        merges = []

        def prune(*args):
            result = real_pass(*args)
            assert not merges
            if exhausted:
                clock.now = 10
            return result

        def merge(**kwargs):
            assert budgets.current_budget() is not None
            assert db.get_session("old") is None
            merges.append(kwargs)
            return real_merge(**kwargs)

        monkeypatch.setattr(db, "_prune_sessions_pass", prune)
        monkeypatch.setattr(db, "_merge_fts_incrementally", merge)
        assert db.prune_sessions(max_seconds=10) == 1
        assert merges == ([] if exhausted else [dict(max_pages=db._FTS_MERGE_MAX_PAGES_PER_INDEX, max_commands=1)])
        assert budgets.current_budget() is None


def test_final_fts_merge_sql_obeys_remaining_pass_budget(tmp_path, monkeypatch):
    import sqlite3

    with closing(SessionDB(tmp_path / "state.db")) as db:
        interrupted = []

        def expensive_probe(table, *, conn):
            try:
                conn.execute("WITH RECURSIVE work(n) AS (SELECT 1 UNION ALL "
                             "SELECT n+1 FROM work WHERE n < 50000000) SELECT sum(n) FROM work").fetchone()
            except sqlite3.OperationalError as exc:
                interrupted.append(str(exc))
                raise
            return True

        with monkeypatch.context() as patch:
            patch.setattr(db, "_fts_table_exists", expensive_probe)
            patch.setattr(db, "_prune_sessions_pass", lambda *args: 0)
            assert db.prune_sessions(max_seconds=0.05) == 0
        assert interrupted == ["interrupted"]
        with db._maintenance_conn() as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        db.set_meta("foreground", "ok")
