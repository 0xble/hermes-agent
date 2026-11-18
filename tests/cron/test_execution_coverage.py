"""Read-only consumers can distinguish complete claim windows from lost history."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from threading import Barrier

import pytest


PROOF_SQL = """
SELECT EXISTS (
    SELECT 1 FROM execution_coverage
    WHERE singleton=1 AND julianday(started_at) < julianday(:window_start)
      AND (SELECT COUNT(*) FROM execution_prune_watermarks) = 2
      AND NOT EXISTS (
          SELECT 1 FROM execution_prune_watermarks
          WHERE unparseable_claimed_at_count > 0
             OR max_pruned_claimed_jd >= julianday(:window_start)
      )
) AS complete
"""


def _read(path, sql, params=()):
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params)]


@pytest.mark.parametrize("legacy", [False, True])
def test_coverage_epoch_starts_once_without_certifying_prior_history(monkeypatch, tmp_path, legacy):
    from cron import executions

    path = tmp_path / "executions.db"
    clock = datetime(2026, 11, 9, 12, tzinfo=UTC)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", path)
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock)
    if legacy:
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("""CREATE TABLE executions (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
                process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,
                status TEXT NOT NULL, claimed_at TEXT NOT NULL, started_at TEXT,
                finished_at TEXT, error TEXT)""")
            conn.execute("""INSERT INTO executions
                (id, job_id, source, process_id, pid, status, claimed_at)
                VALUES ('old', 'job', 'scheduler', 'old', 1, 'claimed', ?)""",
                         ((clock - timedelta(days=20)).isoformat(),))
            conn.commit()
    # Concurrent first openers use the existing ledger lock; none can replace the epoch.
    barrier = Barrier(4)

    def open_ledger(_):
        barrier.wait(timeout=10)
        with executions._transaction() as conn:
            return dict(conn.execute("SELECT * FROM execution_coverage").fetchone())

    with ThreadPoolExecutor(max_workers=4) as pool:
        epochs = list(pool.map(open_ledger, range(4)))
    epoch = epochs[0]
    assert epochs == [epoch] * 4
    assert epoch["started_at"] == clock.isoformat()
    assert epoch["origin"] == ("migration" if legacy else "fresh")
    assert _read(path, PROOF_SQL, {"window_start": (clock + timedelta(seconds=1)).isoformat()}) == [
        {"complete": 1}]
    assert _read(path, PROOF_SQL, {"window_start": clock.isoformat()}) == [{"complete": 0}]
    assert _read(path, PROOF_SQL, {"window_start": (clock - timedelta(seconds=1)).isoformat()}) == [
        {"complete": 0}]
    if legacy:
        assert executions.get_execution("old")["status"] == "claimed"
    clock += timedelta(days=1)
    with executions._transaction() as conn:
        assert dict(conn.execute("SELECT * FROM execution_coverage").fetchone()) == epoch
    # A newly created DB must not inherit the previous file's continuity certificate.
    path.unlink()
    with executions._transaction() as conn:
        recreated = dict(conn.execute("SELECT * FROM execution_coverage").fetchone())
    assert recreated["epoch_id"] != epoch["epoch_id"]
    assert recreated["started_at"] == clock.isoformat()
    assert recreated["origin"] == "fresh"
    assert _read(path, PROOF_SQL, {"window_start": epoch["started_at"]}) == [{"complete": 0}]


@pytest.mark.parametrize("reason", ["age", "cap"])
def test_prune_watermarks_are_atomic_monotone_and_concurrent_safe(monkeypatch, tmp_path, reason):
    from cron import executions

    path = tmp_path / "executions.db"
    clock = datetime(2026, 10, 1, tzinfo=UTC)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", path)
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock)
    with executions._transaction():
        pass
    clock = datetime(2026, 11, 9, 12, tzinfo=UTC)
    # Finish age selects rows, but claim time is the collector's window basis.
    finish = clock - timedelta(days=10 if reason == "age" else 1)
    maximum = clock - timedelta(days=9 if reason == "age" else 1)
    older = maximum - timedelta(hours=1)
    with executions._transaction() as conn:
        conn.executemany("""INSERT INTO executions
            (id, job_id, source, process_id, pid, status, claimed_at, finished_at)
            VALUES (?, 'audit', 'scheduler', 'seed', 1, 'completed', ?, ?)""", [
                ("a", maximum.astimezone(timezone(timedelta(hours=-10))).isoformat(), finish.isoformat()),
                ("b", older.astimezone(timezone(timedelta(hours=14))).isoformat(), finish.isoformat()),
                ("latest", clock.isoformat(), clock.isoformat()),
            ])
    if reason == "cap":
        monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)
    # Failure after deletion must roll back BOTH the delete and the watermark.
    with pytest.raises(RuntimeError, match="abort"):
        with executions._transaction() as conn:
            executions._prune_unlocked(conn)
            assert conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 1
            raise RuntimeError("abort")
    assert len(_read(path, "SELECT id FROM executions")) == 3
    assert all(row["pruned_count"] == 0 for row in _read(path, "SELECT * FROM execution_prune_watermarks"))
    barrier = Barrier(4)

    def prune(_):
        barrier.wait(timeout=10)
        with executions._transaction() as conn:
            executions._prune_unlocked(conn)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(prune, range(4)))
    rows = {row["reason"]: row for row in _read(path, "SELECT * FROM execution_prune_watermarks")}
    watermark = rows[reason]
    assert watermark["pruned_count"] == 2
    assert watermark["unparseable_claimed_at_count"] == 0
    assert watermark["last_pruned_at"] == clock.isoformat()
    expected_jd = _read(path, "SELECT julianday(?) AS jd", (maximum.isoformat(),))[0]["jd"]
    assert watermark["max_pruned_claimed_jd"] == expected_jd
    other = rows["cap" if reason == "age" else "age"]
    assert other["pruned_count"] == 0
    assert other["max_pruned_claimed_jd"] is None
    assert _read(path, "SELECT id FROM executions") == [{"id": "latest"}]
    # Strict >=: an attempt on the window boundary was pruned, so it is unproven.
    assert _read(path, PROOF_SQL, {"window_start": maximum.isoformat()}) == [{"complete": 0}]
    assert _read(path, PROOF_SQL, {"window_start": (maximum + timedelta(seconds=1)).isoformat()}) == [
        {"complete": 1}]
    with executions._transaction() as conn:
        conn.execute("""INSERT INTO executions
            (id, job_id, source, process_id, pid, status, claimed_at, finished_at)
            VALUES ('c', 'audit', 'scheduler', 'seed', 1, 'failed', ?, ?)""",
                     (older.isoformat(), finish.isoformat()))
        executions._prune_unlocked(conn)
    later = _read(path, "SELECT * FROM execution_prune_watermarks WHERE reason=?", (reason,))[0]
    assert later["pruned_count"] == 3
    assert later["max_pruned_claimed_jd"] == expected_jd
    with executions._transaction() as conn:
        conn.execute("""INSERT INTO executions
            (id, job_id, source, process_id, pid, status, claimed_at, finished_at)
            VALUES ('bad-time', 'audit', 'scheduler', 'seed', 1, 'unknown', 'invalid', ?)""",
                     (finish.isoformat(),))
        executions._prune_unlocked(conn)
    final = _read(path, "SELECT * FROM execution_prune_watermarks WHERE reason=?", (reason,))[0]
    assert final["pruned_count"] == 4
    assert final["max_pruned_claimed_jd"] == expected_jd
    assert final["unparseable_claimed_at_count"] == 1
    assert _read(path, PROOF_SQL, {"window_start": clock.isoformat()}) == [{"complete": 0}]
