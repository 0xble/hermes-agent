"""A never-started miss must survive the pruning performed by its own write."""

import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import count
from types import SimpleNamespace
from uuid import UUID

import pytest


NOW = datetime(2026, 10, 29, 17, 21, 28, tzinfo=UTC)
OCCURRENCE = (NOW - timedelta(minutes=11)).isoformat()
NEWEST_ID = "ffffffffffff4fffbfffffffffffffff"


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    from cron import executions

    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    monkeypatch.setattr(executions, "_hermes_now", lambda: NOW)
    ids = count(1)
    monkeypatch.setattr(executions, "uuid", SimpleNamespace(
        uuid4=lambda: UUID(f"00000000-0000-4000-8000-{next(ids):012x}")))
    return executions, home


def _insert(conn, execution_id, stamp, *, status="completed"):
    conn.execute(
        """INSERT INTO executions
           (id, job_id, source, process_id, pid, status, claimed_at, finished_at)
           VALUES (?, 'audit', 'builtin', 'seed', 1, ?, ?, ?)""",
        (execution_id, status, stamp, stamp),
    )


def _committed(home):
    # An independent connection after return proves durability, not just a fetched row.
    with sqlite3.connect(home / "cron" / "executions.db") as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM executions ORDER BY id")]


@pytest.mark.parametrize("cap,seed_offset", [
    pytest.param(100_000, 1, id="low-pressure-newer-same-job"),
    pytest.param(1, -1, id="cap-pressure-miss-is-latest"),
    pytest.param(1, 1, id="cap-pressure-newer-by-one-second"),
    pytest.param(1, 0, id="cap-pressure-equal-timestamp-higher-id"),
])
def test_missed_write_survives_own_prune_and_retry_reuses_it(
    ledger, monkeypatch, cap, seed_offset,
):
    executions, home = ledger
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", cap)
    stamp = (NOW + timedelta(seconds=seed_offset)).isoformat()
    with executions._transaction() as conn:
        _insert(conn, NEWEST_ID, stamp)

    first = executions.record_missed_execution(
        "audit", scheduled_instant=OCCURRENCE, error="never started")
    after_first = _committed(home)
    retry = executions.record_missed_execution(
        "audit", scheduled_instant=OCCURRENCE, error="never started")
    after_retry = _committed(home)

    assert first is not None
    assert first["status"] == "failed"
    assert first["source"] == "scheduler-missed"
    assert first["started_at"] is None
    assert first["scheduled_instant"] == OCCURRENCE
    assert retry == first
    assert after_first == after_retry
    assert [row for row in after_retry if row["source"] == "scheduler-missed"] == [first]
    assert executions.get_execution(first["id"]) == first
    assert executions.list_executions(job_id="audit") == sorted(
        after_retry, key=lambda row: (row["claimed_at"], row["id"]), reverse=True)
    # The cap is best effort: newest and current rows BOTH survive even at cap 1.
    keep_seed = cap > 1 or seed_offset >= 0
    assert len(after_retry) == 1 + int(keep_seed)
    assert (executions.get_execution(NEWEST_ID) is not None) == keep_seed


def test_missed_write_keeps_protections_and_bounded_cleanup(ledger, monkeypatch):
    from cron.scheduler_authority import journal_path

    executions, home = ledger
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)
    stamp = (NOW - timedelta(days=1)).isoformat()
    protected = ("queued", "pending", "delivering", "provisional", "journal", "claimed", "running")
    with executions._transaction() as conn:
        for index in range(1100):
            _insert(conn, f"old-{index:04}", stamp)
        _insert(conn, NEWEST_ID, (NOW + timedelta(seconds=1)).isoformat())
        for name in protected:
            _insert(conn, name, stamp, status=name if name in ("claimed", "running") else "failed")
        conn.execute("UPDATE executions SET delivery_outcome='queued' WHERE id='queued'")
        conn.execute("UPDATE executions SET delivery_status=id WHERE id IN ('pending','delivering')")
        conn.execute("UPDATE executions SET delivery_status='unknown', delivery_status_provisional=1 "
                     "WHERE id='provisional'")
    journal = journal_path("audit", "journal")
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text('{"canonical_settled":false}', encoding="utf-8")
    before = _committed(home)

    current = executions.record_missed_execution(
        "audit", scheduled_instant=OCCURRENCE, error="never started")
    after = _committed(home)
    assert current is not None
    assert len(before) + 1 - len(after) == 500
    assert executions.get_execution(current["id"]) == current
    assert executions.latest_execution("audit")["id"] == NEWEST_ID
    assert all(executions.get_execution(name) is not None for name in protected)
    assert executions.get_execution("old-0000") is None
    assert executions.get_execution("old-0499") is None
    assert executions.get_execution("old-0500") is not None
    assert executions.record_missed_execution(
        "audit", scheduled_instant=OCCURRENCE, error="never started") == current
    assert _committed(home) == after
