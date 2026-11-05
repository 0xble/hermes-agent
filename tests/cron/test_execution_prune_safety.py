"""Retention failures and protected backlogs must not strand terminal writes."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest


NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    from cron import executions

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    monkeypatch.setattr(executions, "_hermes_now", lambda: NOW)
    return executions, tmp_path


@pytest.mark.parametrize("config", [
    pytest.param("cron:\n  execution_retention_days: 10000000\n", id="overflow"),
    pytest.param("model:\n  model_preset: missing-retention-test\n", id="unknown-preset"),
    pytest.param("cron: not-a-mapping\n", id="non-mapping"),
])
@pytest.mark.parametrize("transition", [
    "finish", "recover", "receipt", "dead-owner", "give-up", "missed",
])
def test_terminal_writes_survive_broken_retention_config(ledger, monkeypatch, config, transition):
    executions, home = ledger
    stamp = (NOW - timedelta(days=20)).isoformat()
    with executions._transaction() as conn:
        conn.executemany(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, status, claimed_at, finished_at)
               VALUES (?, 'history', 'builtin', 'seed', 1, 'completed', ?, ?)""",
            ((f"history-{i}", stamp, stamp) for i in range(2)),
        )
    attempt = executions.create_execution("audit", source="builtin")
    executions.mark_execution_running(attempt["id"])
    if transition in ("recover", "receipt", "dead-owner"):
        with executions._transaction() as conn:
            conn.execute("UPDATE executions SET process_id='departed' WHERE id=?", (attempt["id"],))
        monkeypatch.setattr(executions, "_owner_identity", lambda *_: "dead")
        monkeypatch.setattr(executions, "_owner_is_live", lambda *_: False)
    (home / "config.yaml").write_text(config, encoding="utf-8")

    if transition == "finish":
        assert executions.finish_execution(attempt["id"], success=True)["status"] == "completed"
    elif transition == "recover":
        assert executions.recover_interrupted_executions() == 1
    elif transition == "receipt":
        assert executions.recover_receipted_execution(
            attempt["id"], "audit", success=True)["status"] == "completed"
    elif transition == "dead-owner":
        assert executions.terminalize_dead_owner(attempt["id"], reason="worker exited")
    elif transition == "give-up":
        assert executions.give_up_execution(attempt["id"], reason="unknown outcome")
    else:
        attempt = executions.record_missed_execution(
            "missed", scheduled_instant=NOW.isoformat(), error="never started")

    # Read through an independent connection after return: no rolled-back running row.
    with sqlite3.connect(home / "cron" / "executions.db") as conn:
        status, finished = conn.execute(
            "SELECT status, finished_at FROM executions WHERE id=?", (attempt["id"],)).fetchone()
        expired = conn.execute("SELECT id FROM executions WHERE id='history-0'").fetchone()
    # An overflowing window is longer than representable history; other config
    # failures use the default age window rather than silently disabling cleanup.
    assert (expired is not None) == ("10000000" in config)
    expected = {"finish": "completed", "receipt": "completed", "missed": "failed"}
    assert status == expected.get(transition, "unknown")
    assert finished == NOW.isoformat()


@pytest.mark.parametrize("protection", ["latest", "journal", "queued"])
def test_protected_backlog_bounds_prune_work(ledger, monkeypatch, protection):
    from cron.scheduler_authority import journal_path

    executions, home = ledger
    monkeypatch.setattr(executions, "TERMINAL_PRUNE_BATCH_SIZE", 10)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 0)
    stamp = (NOW - timedelta(days=20)).isoformat()
    with executions._transaction() as conn:
        conn.executemany(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, status, claimed_at, finished_at, delivery_outcome)
               VALUES (?, ?, 'builtin', 'seed', 1, 'completed', ?, ?, ?)""",
            ((f"old-{i:04}", f"job-{i}" if protection == "latest" else "audit", stamp, stamp,
              "queued" if protection == "queued" else None) for i in range(2000)),
        )
    if protection == "journal":
        for i in range(2000):
            journal = journal_path("audit", f"old-{i:04}")
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text('{"canonical_settled":false}', encoding="utf-8")
    trigger = executions.create_execution("trigger", source="builtin")
    connect = executions._connect
    steps = 0

    def progress():
        nonlocal steps
        steps += 100
        return 0

    def measured_connect():
        conn = connect()
        conn.set_progress_handler(progress, 100)
        return conn

    monkeypatch.setattr(executions, "_connect", measured_connect)
    assert executions.finish_execution(trigger["id"], success=True)["status"] == "completed"
    # SQLite VM work (including correlated latest-row probes) must stay bounded,
    # even when no row can be deleted. A LIMIT after eligibility is insufficient.
    assert steps < 20_000, f"protected backlog consumed {steps} SQLite steps"
    with sqlite3.connect(home / "cron" / "executions.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 2001
