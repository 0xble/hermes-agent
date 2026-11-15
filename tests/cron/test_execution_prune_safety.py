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


TRANSITIONS = ["finish", "recover", "receipt", "dead-owner", "give-up", "missed"]


def _settle_with_history(executions, monkeypatch, home, transition, *, config=None):
    """Seed 2 rows past the default window, run one terminal transition, and read it back."""
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
    if config is not None:
        (home / "config.yaml").write_text(config, encoding="utf-8")

    transitions = {
        "finish": lambda: executions.finish_execution(attempt["id"], success=True),
        "recover": executions.recover_interrupted_executions,
        "receipt": lambda: executions.recover_receipted_execution(
            attempt["id"], "audit", success=True),
        "dead-owner": lambda: executions.terminalize_dead_owner(
            attempt["id"], reason="worker exited"),
        "give-up": lambda: executions.give_up_execution(attempt["id"], reason="unknown outcome"),
        "missed": lambda: executions.record_missed_execution(
            "missed", scheduled_instant=NOW.isoformat(), error="never started"),
    }
    result = transitions[transition]()
    assert result
    if transition == "missed":
        attempt = result

    # Read through an independent connection after return: no rolled-back running row.
    with sqlite3.connect(home / "cron" / "executions.db") as conn:
        status, finished = conn.execute(
            "SELECT status, finished_at FROM executions WHERE id=?", (attempt["id"],)).fetchone()
        expired = conn.execute("SELECT id FROM executions WHERE id='history-0'").fetchone()
    expected = {"finish": "completed", "receipt": "completed", "missed": "failed"}
    assert status == expected.get(transition, "unknown")
    assert finished == NOW.isoformat()
    return expired is None


@pytest.mark.parametrize("config", [
    pytest.param("cron:\n  execution_retention_days: 10000000\n", id="overflow"),
    pytest.param("model:\n  model_preset: missing-retention-test\n", id="unknown-preset"),
    pytest.param("cron: not-a-mapping\n", id="non-mapping"),
])
@pytest.mark.parametrize("transition", TRANSITIONS)
def test_terminal_writes_survive_broken_retention_config(ledger, monkeypatch, config, transition):
    executions, home = ledger
    expired = _settle_with_history(executions, monkeypatch, home, transition, config=config)
    # An overflowing window is longer than representable history; other config
    # failures use the default age window rather than silently disabling cleanup.
    assert expired == ("10000000" not in config)


@pytest.mark.parametrize("transition", TRANSITIONS)
def test_terminal_writes_survive_unavailable_config_backend(ledger, monkeypatch, transition):
    """``ConfigBackendUnavailable`` is a ``SystemExit``: it must not escape the terminal write."""
    import sys

    import hermes_cli.config
    from hermes_cli.config_backend import ConfigBackendUnavailable

    executions, home = ledger
    real_load = hermes_cli.config.load_config_readonly

    def unavailable_for_retention():
        # Only the retention lookup inside the terminal transaction sees the outage;
        # opening the ledger reads config too and is not under test here.
        if sys._getframe(1).f_code is executions._retention_cutoff.__code__:
            raise ConfigBackendUnavailable("config plane unreachable")
        return real_load()

    monkeypatch.setattr(hermes_cli.config, "load_config_readonly", unavailable_for_retention)
    # The backend may hold a longer window than the default, so age expiry waits for it.
    assert _settle_with_history(executions, monkeypatch, home, transition) is False


@pytest.mark.parametrize("protection", ["journal", "queued", "pending", "delivering", "provisional"])
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
            ((f"old-{i:04}", "audit", stamp, stamp,
              "queued" if protection == "queued" else None) for i in range(2000)),
        )
        if protection in ("pending", "delivering"):
            conn.execute("UPDATE executions SET delivery_status=?", (protection,))
        if protection == "provisional":
            conn.execute("UPDATE executions SET delivery_status_provisional=1")
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

    # These obligations have no guaranteed expiry. If their owner settles them,
    # later bounded batches must actually drain the now-eligible backlog.
    monkeypatch.setattr(executions, "_connect", connect)
    if protection == "journal":
        for i in range(2000):
            journal_path("audit", f"old-{i:04}").unlink()
    else:
        with executions._transaction() as conn:
            conn.execute("UPDATE executions SET delivery_outcome=NULL, delivery_status=NULL, "
                         "delivery_status_provisional=0")
    monkeypatch.setattr(executions, "TERMINAL_PRUNE_BATCH_SIZE", 500)
    for i in range(5):
        monkeypatch.setattr(executions, "_hermes_now", lambda i=i: NOW + timedelta(seconds=i))
        trigger = executions.create_execution("trigger", source="builtin")
        assert executions.finish_execution(trigger["id"], success=True)
    with sqlite3.connect(home / "cron" / "executions.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 2


@pytest.mark.parametrize("policy, expired_count", [("age", 30), ("backstop", 30), ("backstop", 1)])
def test_pruning_converges_past_permanent_latest_attempts(ledger, monkeypatch, policy, expired_count):
    executions, home = ledger
    monkeypatch.setattr(executions, "TERMINAL_PRUNE_BATCH_SIZE", 10)
    # More permanent latest rows than even a default 500-row batch's page.
    # Singletons include one-shot/removed jobs: no future attempt releases them.
    singleton_count = 2001
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS",
                        100_000 if policy == "age" else singleton_count + 1)
    if policy == "backstop":
        (home / "config.yaml").write_text("cron:\n  execution_retention_days: 0\n", encoding="utf-8")
    oldest = (NOW - timedelta(days=30)).isoformat()
    expired = (NOW - timedelta(days=20)).isoformat()
    with executions._transaction() as conn:
        conn.executemany(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, status, claimed_at, finished_at)
               VALUES (?, ?, 'builtin', 'seed', 1, 'completed', ?, ?)""",
            [(f"singleton-{i:04}", f"once-{i}", oldest, oldest) for i in range(singleton_count)]
            + [(f"expired-{i:04}", "recurring", expired, expired) for i in range(expired_count)],
        )
    # The recurring job's latest attempt is live, so all its terminal rows qualify.
    current = executions.create_execution("recurring", source="builtin")
    for i in range(4):
        monkeypatch.setattr(executions, "_hermes_now", lambda i=i: NOW + timedelta(seconds=i))
        trigger = executions.create_execution("trigger", source="builtin")
        assert executions.finish_execution(trigger["id"], success=True)
    with sqlite3.connect(home / "cron" / "executions.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM executions WHERE id LIKE 'expired-%'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM executions WHERE id LIKE 'singleton-%'").fetchone()[0] == singleton_count
        if policy == "backstop":
            assert conn.execute("SELECT COUNT(*) FROM executions WHERE status='completed'").fetchone()[0] == singleton_count + 1
    assert executions.get_execution(current["id"])["status"] == "claimed"


@pytest.mark.parametrize("protection", ["journal", "queued", "pending", "delivering", "provisional"])
def test_protected_oldest_page_cannot_starve_newer_eligible_history(ledger, monkeypatch, protection):
    from cron.scheduler_authority import journal_path

    executions, home = ledger
    monkeypatch.setattr(executions, "TERMINAL_PRUNE_BATCH_SIZE", 10)
    older = (NOW - timedelta(days=30)).isoformat()
    expired = (NOW - timedelta(days=20)).isoformat()
    # Exactly one full candidate page (4x the batch) of older protected rows, then
    # eligible rows of a job whose latest attempt is still live.
    with executions._transaction() as conn:
        conn.executemany(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, status, claimed_at, finished_at,
                delivery_outcome, delivery_status, delivery_status_provisional)
               VALUES (?, 'audit', 'builtin', 'seed', 1, 'completed', ?, ?, ?, ?, ?)""",
            ((f"protected-{i:02}", older, older,
              "queued" if protection == "queued" else None,
              protection if protection in ("pending", "delivering") else None,
              int(protection == "provisional")) for i in range(40)),
        )
        conn.executemany(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, status, claimed_at, finished_at)
               VALUES (?, 'recurring', 'builtin', 'seed', 1, 'completed', ?, ?)""",
            ((f"eligible-{i}", expired, expired) for i in range(5)),
        )
    if protection == "journal":
        for i in range(40):
            journal = journal_path("audit", f"protected-{i:02}")
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text('{"canonical_settled":false}', encoding="utf-8")
    executions.create_execution("recurring", source="builtin")
    for i in range(3):
        monkeypatch.setattr(executions, "_hermes_now", lambda i=i: NOW + timedelta(seconds=i))
        trigger = executions.create_execution("trigger", source="builtin")
        assert executions.finish_execution(trigger["id"], success=True)
    with sqlite3.connect(home / "cron" / "executions.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM executions WHERE id LIKE 'eligible-%'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM executions WHERE id LIKE 'protected-%'").fetchone()[0] == 40


@pytest.mark.parametrize("undated_protected", [
    # The reviewer's case: one protected unparseable row sits in the first page, the
    # cursor then advances among dated rows, and the row's journal is released.
    pytest.param(1, id="released-behind-dated-cursor"),
    # More protected unparseable rows than a page: the cursor must resume inside them.
    pytest.param(12, id="full-undated-page"),
])
def test_unparseable_age_rows_stay_in_the_cap_walk(ledger, monkeypatch, undated_protected):
    from cron.scheduler_authority import journal_path

    executions, home = ledger
    monkeypatch.setattr(executions, "TERMINAL_PRUNE_BATCH_SIZE", 2)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 0)
    (home / "config.yaml").write_text("cron:\n  execution_retention_days: 0\n", encoding="utf-8")
    older = (NOW - timedelta(days=30)).isoformat()
    undated = [f"undated-{i:02}" for i in range(undated_protected)]
    dated = [f"protected-{i:02}" for i in range(20)]
    with executions._transaction() as conn:
        # Both times unreadable: julianday() is NULL, so the age key is NULL.
        conn.executemany(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, status, claimed_at, finished_at)
               VALUES (?, 'recurring', 'builtin', 'seed', 1, 'completed', 'not-a-time', 'not-a-time')""",
            ((row_id,) for row_id in undated),
        )
        conn.executemany(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, status, claimed_at, finished_at)
               VALUES (?, 'audit', 'builtin', 'seed', 1, 'completed', ?, ?)""",
            ((row_id, older, older) for row_id in dated),
        )
    for job_id, row_id in [("recurring", r) for r in undated] + [("audit", r) for r in dated]:
        journal = journal_path(job_id, row_id)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text('{"canonical_settled":false}', encoding="utf-8")
    executions.create_execution("recurring", source="builtin")

    def settle(i):
        monkeypatch.setattr(executions, "_hermes_now", lambda: NOW + timedelta(seconds=i))
        trigger = executions.create_execution("trigger", source="builtin")
        assert executions.finish_execution(trigger["id"], success=True)

    settle(0)
    with sqlite3.connect(home / "cron" / "executions.db") as conn:
        assert conn.execute(
            "SELECT scan_after_id FROM execution_prune_watermarks WHERE reason='cap'"
        ).fetchone()[0] is not None
    # The undated rows' owner settles after the cursor has moved past them.
    for row_id in undated:
        journal_path("recurring", row_id).unlink()
    # Each call examines at most one 8-row page, so a full cycle spans several calls.
    for i in range(1, 21):
        settle(i)
    with sqlite3.connect(home / "cron" / "executions.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM executions WHERE id LIKE 'undated-%'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM executions WHERE id LIKE 'protected-%'").fetchone()[0] == 20
        assert conn.execute(
            "SELECT unparseable_claimed_at_count FROM execution_prune_watermarks WHERE reason='cap'"
        ).fetchone()[0] == undated_protected
