"""Weekly audit history survives fleet volume, with bounded hot-path cleanup."""

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture(params=[
    datetime(2026, 10, 29, 18, tzinfo=UTC),
    datetime(2026, 11, 5, 12, tzinfo=ZoneInfo("America/New_York")),
])
def ledger(monkeypatch, tmp_path, request):
    from cron import executions

    clock = request.param
    now = clock.astimezone(UTC)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock)
    return executions, now


def _seed(executions, *, count, claimed_at, finished_at):
    with executions._transaction() as conn:
        conn.executemany(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, status, claimed_at, finished_at)
               VALUES (?, 'audit', 'builtin', 'seed', 1, ?, ?, ?)""",
            ((f"seed-{index:04}", ("completed", "failed", "unknown")[index % 3],
              claimed_at, finished_at) for index in range(count)),
        )


def _count(executions):
    with executions._transaction() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM executions WHERE status IN ('completed','failed','unknown')"
        ).fetchone()[0]


def test_weekly_window_keeps_over_1000_terminal_attempts(ledger):
    executions, now = ledger
    # Different offsets must be compared as instants, not ISO text. A newly
    # finished long-running attempt is young even if its claim predates the window.
    _seed(executions, count=1100, claimed_at=(now - timedelta(days=20)).isoformat(),
          finished_at=(now - timedelta(days=7, hours=23)).astimezone(
              timezone(timedelta(hours=-10))).isoformat())
    with executions._transaction() as conn:
        conn.execute("UPDATE executions SET finished_at=? WHERE id='seed-0000'",
                     ((now - timedelta(days=8)).isoformat(),))
        conn.execute("UPDATE executions SET finished_at=? WHERE id='seed-0001'",
                     ((now - timedelta(days=8, seconds=1)).astimezone(
                         timezone(timedelta(hours=14))).isoformat(),))
    fresh = executions.create_execution("trigger", source="builtin")
    assert executions.finish_execution(fresh["id"], success=True) is not None
    assert _count(executions) == 1100
    assert executions.get_execution("seed-0000") is not None
    assert executions.get_execution("seed-0001") is None
    assert executions.get_execution("seed-0002")["status"] == "unknown"


@pytest.mark.parametrize("reason", ["expired", "backstop", "both"])
def test_pruning_converges_without_an_unbounded_delete(ledger, monkeypatch, reason):
    executions, now = ledger
    cap = 100_000 if reason == "expired" else 100
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", cap)
    stamp = (now - timedelta(days=1 if reason == "backstop" else 9)).isoformat()
    _seed(executions, count=1100, claimed_at=stamp, finished_at=stamp)
    if reason == "both":
        with executions._transaction() as conn:
            conn.execute("UPDATE executions SET finished_at=? WHERE id>='seed-0550'",
                         ((now - timedelta(days=1)).isoformat(),))
    # Legacy terminal rows without a finish time still age out by their claim.
    if reason == "expired":
        with executions._transaction() as conn:
            conn.execute("UPDATE executions SET finished_at=NULL WHERE id='seed-0000'")
    inflight = executions.create_execution("live", source="builtin")
    executions.mark_execution_running(inflight["id"])
    with executions._transaction() as conn:
        conn.execute("UPDATE executions SET claimed_at=? WHERE id=?",
                     ((now - timedelta(days=30)).isoformat(), inflight["id"]))

    for index in range(4):
        before = _count(executions)
        trigger = executions.create_execution(f"trigger-{index}", source="builtin")
        assert executions.finish_execution(trigger["id"], success=True) is not None
        deleted = before + 1 - _count(executions)
        assert 0 <= deleted <= 500
    # The audit job's latest attempt remains available even after its window expires.
    assert _count(executions) == (5 if reason == "expired" else cap)
    assert executions.latest_execution("audit")["id"] == "seed-1099"
    assert executions.get_execution(inflight["id"])["status"] == "running"
    assert executions.get_execution("seed-0000") is None


@pytest.mark.parametrize("days", [0, 40])
def test_configured_window_preserves_long_history_but_keeps_backstop(
    ledger, monkeypatch, tmp_path, days,
):
    from agent.secret_scope import is_multiplex_active, set_multiplex_active
    from cron.scheduler_provider import _profile_cron_scope

    executions, now = ledger
    (tmp_path / "config.yaml").write_text(
        f"cron:\n  execution_retention_days: {days}\n", encoding="utf-8")
    stamp = (now - timedelta(days=30)).isoformat()
    _seed(executions, count=3, claimed_at=stamp, finished_at=stamp)
    fresh = executions.create_execution("trigger", source="builtin")
    assert executions.finish_execution(fresh["id"], success=True) is not None
    assert executions.get_execution("seed-0000") is not None

    # A -> B -> A in one multiplexed process: B retains the default window,
    # while returning to A must restore its configured long/disabled window.
    other = tmp_path / "other"
    other.mkdir()
    was_active = is_multiplex_active()
    set_multiplex_active(True)
    try:
        for home in (tmp_path, other, tmp_path):
            with _profile_cron_scope(home):
                if home == other:
                    _seed(executions, count=3, claimed_at=stamp, finished_at=stamp)
                fresh = executions.create_execution("trigger", source="builtin")
                assert executions.finish_execution(fresh["id"], success=True) is not None
                assert (executions.get_execution("seed-0000") is None) == (home == other)
    finally:
        set_multiplex_active(was_active)

    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 3)
    fresh = executions.create_execution("trigger", source="builtin")
    assert executions.finish_execution(fresh["id"], success=True) is not None
    assert _count(executions) == 3
    assert executions.get_execution("seed-0000") is None


@pytest.mark.parametrize("policy", ["age", "backstop"])
@pytest.mark.parametrize("protected", [
    "claimed", "running", "latest", "journal", "pending", "delivering", "provisional", "queued",
])
def test_pruning_preserves_current_state_and_unsettled_work(
    ledger, monkeypatch, protected, policy,
):
    from cron.scheduler_authority import journal_path

    executions, now = ledger
    stamp = (now - timedelta(days=20)).astimezone(timezone(timedelta(hours=14))).isoformat()
    _seed(executions, count=3, claimed_at=stamp, finished_at=stamp)
    if policy == "backstop":
        monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 0)
    journal = journal_path("audit", "seed-0000")
    with executions._transaction() as conn:
        if protected in ("claimed", "running"):
            conn.execute("UPDATE executions SET status=? WHERE id='seed-0000'", (protected,))
        elif protected == "latest":
            # Chronologically newer, lexically older. Match the cron list readers' ordering.
            newer = (now - timedelta(days=20) + timedelta(hours=1)).astimezone(
                timezone(timedelta(hours=-10))).isoformat()
            conn.execute("UPDATE executions SET claimed_at=? WHERE id='seed-0000'", (newer,))
        elif protected == "journal":
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(
                '{"params":{"job_id":"audit","request_id":"seed-0000"},'
                '"worker_result":{},"canonical_settled":false}', encoding="utf-8")
        elif protected == "queued":
            conn.execute("UPDATE executions SET delivery_outcome='queued' WHERE id='seed-0000'")
        else:
            status = "unknown" if protected == "provisional" else protected
            conn.execute(
                "UPDATE executions SET delivery_status=?, delivery_status_provisional=? "
                "WHERE id='seed-0000'", (status, int(protected == "provisional")))
    latest = executions.latest_executions(["audit"])["audit"]
    fresh = executions.create_execution("trigger", source="builtin")
    assert executions.finish_execution(fresh["id"], success=True) is not None
    assert executions.get_execution("seed-0000") is not None
    assert executions.get_execution("seed-0001") is None
    assert executions.latest_execution("audit") == latest
    if protected == "journal":
        journal.unlink()
        fresh = executions.create_execution("trigger", source="builtin")
        assert executions.finish_execution(fresh["id"], success=True) is not None
        assert executions.get_execution("seed-0000") is None
