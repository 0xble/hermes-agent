"""Durable at-most-once delivery handoff for restart-safe cron workers."""

from __future__ import annotations

import sqlite3
import time
from unittest.mock import Mock

import pytest


def test_gated_missing_execution_expires_without_sending_and_uses_one_ledger_read(tmp_path, monkeypatch):
    from cron import delivery_queue as queue, executions
    from datetime import timedelta
    from hermes_time import now

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    running = executions.create_execution("job-live", source="builtin")
    executions.mark_execution_running(running["id"])
    for execution_id in ("missing-old", "missing-young", running["id"]):
        queue.enqueue(execution_id, {"id": "job"}, "message", terminal_gate=True)
    with queue._transaction() as conn:
        conn.execute("UPDATE deliveries SET created_at=? WHERE execution_id=?",
                     ((now() - timedelta(days=2)).isoformat(), "missing-old"))
    original_connect = executions._connect
    calls = []
    def connect():
        calls.append(True)
        return original_connect()
    monkeypatch.setattr(executions, "_connect", connect)
    assert queue.claim_next() is None
    assert len(calls) == 1
    assert queue.get_status("missing-old")["status"] == "suppressed"
    assert queue.get_status("missing-young")["status"] == "pending"
    assert queue.get_status(running["id"])["status"] == "pending"
    monkeypatch.setattr(executions, "_connect", Mock(side_effect=OSError("ledger offline")))
    assert queue.claim_next() is None
    assert queue.get_status("missing-young")["status"] == "pending"


def test_gated_failure_notice_preserves_pre_timeout_failure_text(tmp_path, monkeypatch):
    """A genuine failure alert is preferable to suppressing it for a changed reason.

    Its text may describe the pre-timeout failure; the execution ledger retains
    the watchdog's authoritative error for status/history.
    """
    from cron import delivery_queue as queue, executions
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    run = executions.create_execution("job-failure", source="builtin")
    executions.mark_execution_running(run["id"])
    queue.enqueue(run["id"], {"id": "job-failure"}, "original failure",
                  for_failure=True, terminal_gate=True)
    executions.finish_execution(run["id"], success=False, error="watchdog timeout")
    sent = Mock(return_value=None)
    assert queue.drain(sent) == 1
    sent.assert_called_once_with({"id": "job-failure"}, "original failure", True)
    assert executions.get_execution(run["id"])["error"] == "watchdog timeout"


def test_pending_delivery_is_claimed_and_sent_once(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-1", {"id": "job-1"}, "brief")
    send = Mock(return_value=None)

    assert queue.drain(send) == 1
    assert queue.drain(send) == 0
    send.assert_called_once_with({"id": "job-1"}, "brief", False)
    status = queue.get_status("exec-1")
    assert status["status"] == "delivered"
    assert status["job_json"] == "{}"
    assert status["content"] == ""


def test_pending_deliveries_are_claimed_in_instant_order_across_dst_fall_back(
    tmp_path, monkeypatch
):
    """01:10-05:00 is 20 minutes after 01:50-04:00 but sorts first as text."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    new_york = ZoneInfo("America/New_York")
    monkeypatch.setattr(
        queue, "_hermes_now", lambda: datetime(2026, 11, 1, 1, 50, tzinfo=new_york, fold=0)
    )
    queue.enqueue("exec-z-earlier", {"id": "job-1"}, "first")
    monkeypatch.setattr(
        queue, "_hermes_now", lambda: datetime(2026, 11, 1, 1, 10, tzinfo=new_york, fold=1)
    )
    queue.enqueue("exec-a-later", {"id": "job-2"}, "second")

    assert queue.claim_next()["execution_id"] == "exec-z-earlier"
    assert queue.claim_next()["execution_id"] == "exec-a-later"


def test_terminal_delivery_retention_is_bounded(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    monkeypatch.setattr(queue, "MAX_TERMINAL_DELIVERIES", 2, raising=False)
    for index in range(4):
        execution_id = f"exec-{index}"
        queue.enqueue(execution_id, {"id": f"job-{index}"}, f"brief-{index}")
        assert queue.claim_next()["execution_id"] == execution_id
        assert queue._finish(execution_id, error=None)

    # Pruning may discard verbose outcome rows, but never the durable
    # idempotency tombstone for an execution that could be replayed later.
    pruned = queue.get_status("exec-0")
    assert pruned is not None
    assert pruned["status"] == "delivered"
    assert queue.get_status("exec-1")["status"] == "delivered"
    assert queue.get_status("exec-2")["status"] == "delivered"
    assert queue.get_status("exec-3")["status"] == "delivered"

    # Pruning may discard verbose outcome rows, but never the durable
    # idempotency tombstone for an execution that could be replayed later.
    queue.enqueue("exec-0", {"id": "job-replayed"}, "duplicate brief")
    send = Mock(return_value=None)
    assert queue.drain(send) == 0
    send.assert_not_called()


def test_pruning_preserves_projected_tombstone(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    monkeypatch.setattr(queue, "MAX_TERMINAL_DELIVERIES", 0, raising=False)
    queue.enqueue("exec-projected", {"id": "job"}, "brief")
    assert queue.claim_next()["execution_id"] == "exec-projected"
    assert queue._finish("exec-projected", error=None)

    with sqlite3.connect(queue.queue_path()) as conn:
        projected = conn.execute(
            "SELECT projected FROM delivery_tombstones WHERE execution_id=?",
            ("exec-projected",),
        ).fetchone()
    assert projected == (1,)


def test_failure_delivery_lane_survives_durable_handoff(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue(
        "exec-failure",
        {"id": "job-failure", "failure_deliver": "local"},
        "failed",
        for_failure=True,
    )
    send = Mock(return_value=None)

    assert queue.drain(send) == 1
    send.assert_called_once_with(
        {"id": "job-failure", "failure_deliver": "local"},
        "failed",
        True,
    )


def test_legacy_queue_schema_adds_failure_lane_before_enqueue(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    db = tmp_path / "deliveries.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE deliveries (
                 execution_id TEXT PRIMARY KEY,
                 job_json TEXT NOT NULL,
                 content TEXT NOT NULL,
                 status TEXT NOT NULL,
                 owner_process_id TEXT,
                 owner_pid INTEGER,
                 owner_started_at INTEGER,
                 created_at TEXT NOT NULL,
                 finished_at TEXT,
                 error TEXT
               )"""
        )
    monkeypatch.setattr(queue, "DELIVERY_DB", db)

    queue.enqueue(
        "exec-migrated",
        {"id": "job-migrated"},
        "failed",
        for_failure=True,
    )

    assert queue.get_status("exec-migrated")["for_failure"] == 1


def test_wait_timeout_marks_inflight_delivery_unknown_without_retry(
    tmp_path, monkeypatch
):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-inflight", {"id": "job-inflight"}, "result")
    assert queue.claim_next() is not None

    error = queue.enqueue_and_wait(
        "exec-inflight", {"id": "job-inflight"}, "result", timeout=0
    )

    assert error is not None
    assert "outcome is unknown" in error
    status = queue.get_status("exec-inflight")
    assert status is not None
    assert status["status"] == "unknown"
    send = Mock(return_value=None)
    assert queue.drain(send) == 0
    send.assert_not_called()


def test_dead_delivery_owner_becomes_unknown_and_is_not_retried(
    tmp_path, monkeypatch
):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-1", {"id": "job-1"}, "brief")
    assert queue.claim_next() is not None
    monkeypatch.setattr(queue, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(queue, "_owner_is_live", lambda _pid, _started: False)

    assert queue.recover_abandoned() == 1
    send = Mock()
    assert queue.drain(send) == 0
    send.assert_not_called()
    assert queue.get_status("exec-1")["status"] == "unknown"


_DELIVERY_FROM_STATES = (
    None, "pending", "delivering", "unknown_provisional", "unknown_terminal",
    "delivered", "failed", "suppressed",
)
_DELIVERY_TARGETS = ("pending", "delivering", "unknown", "delivered", "failed", "suppressed")


@pytest.mark.parametrize("from_state", _DELIVERY_FROM_STATES)
@pytest.mark.parametrize("to_status", _DELIVERY_TARGETS)
def test_execution_delivery_transition_matrix(tmp_path, monkeypatch, from_state, to_status):
    """Every source/target pair executes the real conditional SQLite UPDATE."""
    from cron import executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    run = executions.create_execution("matrix", source="builtin")
    original_status = "unknown" if from_state in ("unknown_provisional", "unknown_terminal") else from_state
    original_provisional = int(from_state == "unknown_provisional")
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET delivery_status=?, delivery_status_provisional=? WHERE id=?",
            (original_status, original_provisional, run["id"]),
        )
    mutable = {None, "pending", "delivering", "unknown_provisional"}
    allowed = (
        from_state in ({None, "pending", "unknown_provisional"} if to_status == "pending" else
                       {None, "pending", "delivering", "unknown_provisional"} if to_status in ("delivering", "unknown") else
                       mutable | {"unknown_terminal"})
    )
    executions.record_delivery_status(run["id"], to_status)
    actual = executions.get_execution(run["id"])
    assert (actual["delivery_status"], actual["delivery_status_provisional"]) == (
        to_status if allowed else original_status,
        0 if allowed else original_provisional,
    )


def test_stale_delivering_cannot_replace_projected_unknown(tmp_path, monkeypatch):
    """Idempotent enqueue reads delivering, then a wait timeout projects unknown first."""
    from cron import delivery_queue as queue, executions
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        monkeypatch.setattr(queue, "DELIVERY_DB", home / "cron" / "deliveries.db")
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", home / "cron" / "executions.db")
        run = executions.create_execution("job-racing-delivering", source="builtin")
        queue.enqueue(run["id"], {"id": "job-racing-delivering"}, "result")
        assert queue.claim_next()["execution_id"] == run["id"]
        original_reflect = queue._reflect_execution_delivery

        def terminalize_after_enqueue_commit(execution_id, status):
            if status != "delivering":
                return original_reflect(execution_id, status)
            # The outer enqueue already read delivering; restore normal terminal projection.
            monkeypatch.setattr(queue, "_reflect_execution_delivery", original_reflect)
            assert "unknown" in queue._terminalize_wait_timeout(execution_id)
            assert queue.get_status(execution_id)["status"] == "unknown"
            assert executions.get_execution(execution_id)["delivery_status"] == "unknown"
            with sqlite3.connect(queue.queue_path()) as conn:
                assert conn.execute(
                    "SELECT projected FROM deliveries WHERE execution_id=?", (execution_id,)
                ).fetchone() == (1,)
            original_reflect(execution_id, status)

        monkeypatch.setattr(queue, "_reflect_execution_delivery", terminalize_after_enqueue_commit)
        queue.enqueue(run["id"], {"id": "job-racing-delivering"}, "result")
        assert queue.reconcile_terminal_deliveries() == 0
        assert executions.get_execution(run["id"])["delivery_status"] == "unknown"
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("terminal", ("unknown", "delivered", "failed", "suppressed"))
def test_stale_enqueue_pending_cannot_replace_projected_terminal_receipt(
    tmp_path, monkeypatch, terminal
):
    from cron import delivery_queue as queue, executions
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        monkeypatch.setattr(queue, "DELIVERY_DB", home / "cron" / "deliveries.db")
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", home / "cron" / "executions.db")
        run = executions.create_execution("job-racing-projection", source="builtin")
        original_reflect = queue._reflect_execution_delivery

        def terminalize_after_enqueue_commit(execution_id, status):
            assert execution_id == run["id"]
            if status != "pending":
                return original_reflect(execution_id, status)
            # enqueue() has committed the queue row but has not projected pending.
            # A second connection claims and terminalizes it, projecting the receipt.
            assert queue.claim_next()["execution_id"] == execution_id
            if terminal == "unknown":
                assert "unknown" in queue._terminalize_wait_timeout(execution_id)
            else:
                assert queue._finish(
                    execution_id,
                    error="send failed" if terminal == "failed" else None,
                    suppressed=terminal == "suppressed",
                )
            assert queue.get_status(execution_id)["status"] == terminal
            assert executions.get_execution(execution_id)["delivery_status"] == terminal
            with sqlite3.connect(queue.queue_path()) as conn:
                assert conn.execute(
                    "SELECT projected FROM deliveries WHERE execution_id=?", (execution_id,)
                ).fetchone()[0] == 1
            original_reflect(execution_id, status)  # stale pending projection resumes

        monkeypatch.setattr(queue, "_reflect_execution_delivery", terminalize_after_enqueue_commit)
        queue.enqueue(run["id"], {"id": "job-racing-projection"}, "result")
        assert queue.reconcile_terminal_deliveries() == 0
        assert executions.get_execution(run["id"])["delivery_status"] == terminal
    finally:
        reset_hermes_home_override(token)


def test_terminal_recovery_and_timeout_project_execution_status(tmp_path, monkeypatch):
    from cron import delivery_queue as queue, executions
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        for transition in ("restart", "timeout"):
            run = executions.create_execution(f"job-{transition}", source="builtin")
            queue.enqueue(run["id"], {"id": f"job-{transition}"}, "result")
            assert queue.claim_next()["execution_id"] == run["id"]
            assert executions.get_execution(run["id"])["delivery_status"] == "pending"
            if transition == "restart":
                monkeypatch.setattr(queue, "_PROCESS_ID", "replacement-gateway")
                monkeypatch.setattr(queue, "_owner_is_live", lambda _pid, _started: False)
                assert queue.recover_abandoned() == 1
            else:
                assert "unknown" in queue._terminalize_wait_timeout(run["id"])
            assert queue.get_status(run["id"])["status"] == "unknown"
            assert executions.get_execution(run["id"])["delivery_status"] == "unknown"
    finally:
        reset_hermes_home_override(token)


def test_terminal_queue_commit_reconciles_execution_projection_without_resend(
    tmp_path, monkeypatch
):
    from cron import delivery_queue as queue, executions
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        monkeypatch.setattr(queue, "DELIVERY_DB", home / "cron" / "deliveries.db")
        monkeypatch.setattr(queue, "MAX_TERMINAL_DELIVERIES", 0, raising=False)
        run = executions.create_execution("job-reconcile", source="builtin")
        queue.enqueue(run["id"], {"id": "job-reconcile"}, "result")
        send = Mock(return_value=None)
        original_reflect = queue._reflect_execution_delivery
        monkeypatch.setattr(
            queue,
            "_reflect_execution_delivery",
            Mock(side_effect=OSError("ledger unavailable")),
        )

        with pytest.raises(OSError, match="ledger unavailable"):
            queue.drain(send)

        assert send.call_count == 1
        assert executions.get_execution(run["id"])["delivery_status"] == "pending"
        assert queue.get_status(run["id"])["status"] == "delivered"

        monkeypatch.setattr(queue, "_reflect_execution_delivery", original_reflect)
        assert queue.drain(send) == 0
        send.assert_called_once_with({"id": "job-reconcile"}, "result", False)
        assert executions.get_execution(run["id"])["delivery_status"] == "delivered"
        assert queue.get_status(run["id"])["status"] == "delivered"
    finally:
        reset_hermes_home_override(token)


def test_terminal_projection_retries_after_execution_ledger_failure(
    tmp_path, monkeypatch
):
    from cron import delivery_queue as queue, executions
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        monkeypatch.setattr(queue, "DELIVERY_DB", home / "cron" / "deliveries.db")
        run = executions.create_execution("job-retry-projection", source="builtin")
        queue.enqueue(run["id"], {"id": "job-retry-projection"}, "result")
        assert queue.claim_next() is not None
        original_reflect = queue._reflect_terminal_deliveries
        monkeypatch.setattr(
            queue,
            "_reflect_terminal_deliveries",
            Mock(side_effect=OSError("ledger unavailable")),
        )
        with pytest.raises(OSError, match="ledger unavailable"):
            queue._finish(run["id"], error=None)

        original_connect = executions._connect
        monkeypatch.setattr(queue, "_reflect_terminal_deliveries", original_reflect)
        monkeypatch.setattr(executions, "_connect", Mock(side_effect=OSError("ledger unavailable")))
        with pytest.raises(OSError, match="ledger unavailable"):
            queue.reconcile_terminal_deliveries()

        with sqlite3.connect(queue.queue_path()) as conn:
            projection = conn.execute(
                "SELECT projected FROM deliveries WHERE execution_id=?", (run["id"],)
            ).fetchone()
            assert projection is not None
            assert projection[0] == 0

        monkeypatch.setattr(executions, "_connect", original_connect)
        assert queue.reconcile_terminal_deliveries() == 1
        assert executions.get_execution(run["id"])["delivery_status"] == "delivered"
        assert queue.reconcile_terminal_deliveries() == 0
    finally:
        reset_hermes_home_override(token)


def test_terminal_receipt_reconciles_unknown_commit_gap(tmp_path, monkeypatch):
    from cron import delivery_queue as queue, executions
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        monkeypatch.setattr(queue, "DELIVERY_DB", home / "cron" / "deliveries.db")
        run = executions.create_execution("gap", source="builtin")
        executions.mark_execution_running(run["id"])
        with executions._transaction() as conn:
            conn.execute("UPDATE executions SET delivery_status='unknown' WHERE id=?", (run["id"],))
        # Crash after the queue's terminal commit, before it projected to ledger.
        with queue._transaction() as conn:
            conn.execute(
                "INSERT INTO deliveries (execution_id, job_json, content, status, created_at, finished_at) "
                "VALUES (?, '{}', '', 'delivered', ?, ?)",
                (run["id"], "2026-09-26T00:00:00+00:00", "2026-09-26T00:00:01+00:00"),
            )
        assert queue.drain(lambda *_: pytest.fail("must not resend")) == 0
        assert executions.get_execution(run["id"])["delivery_status"] == "delivered"
        assert queue.drain(lambda *_: pytest.fail("must not resend")) == 0
    finally:
        reset_hermes_home_override(token)


def test_terminal_receipt_reconciliation_uses_projection_index_and_skips_projected_rows(
    tmp_path, monkeypatch
):
    from cron import delivery_queue as queue, executions
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        delivery_db = home / "cron" / "deliveries.db"
        execution_db = home / "cron" / "executions.db"
        monkeypatch.setattr(queue, "DELIVERY_DB", delivery_db)
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", execution_db)
        run = executions.create_execution("job-indexed", source="builtin")

        with queue._transaction() as conn:
            conn.execute(
                "INSERT INTO deliveries "
                "(execution_id, job_json, content, status, created_at, finished_at) "
                "VALUES (?, '{}', '', 'delivered', ?, ?)",
                (run["id"], "2026-09-26T00:00:00+00:00", "2026-09-26T00:00:01+00:00"),
            )

        assert queue.reconcile_terminal_deliveries() == 1
        assert queue.reconcile_terminal_deliveries() == 0
        with sqlite3.connect(delivery_db) as conn:
            assert conn.execute(
                "SELECT projected FROM deliveries WHERE execution_id=?", (run["id"],)
            ).fetchone()[0] == 1
    finally:
        reset_hermes_home_override(token)


def test_reconcile_10k_tombstones_under_one_second(tmp_path, monkeypatch):
    from cron import delivery_queue as queue, executions
    from hermes_cli.sqlite_util import transaction
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        delivery_db = home / "cron" / "deliveries.db"
        execution_db = home / "cron" / "executions.db"
        monkeypatch.setattr(queue, "DELIVERY_DB", delivery_db)
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", execution_db)
        count = 10_000
        ids = [f"exec-tombstone-{index}" for index in range(count)]
        ledger = executions._connect()
        with transaction(ledger) as conn:
            conn.executemany(
                "INSERT INTO executions "
                "(id, job_id, source, process_id, pid, status, claimed_at) "
                "VALUES (?, ?, 'builtin', 'test', 1, 'completed', ?)",
                [(execution_id, execution_id, "2026-09-26T00:00:00+00:00") for execution_id in ids],
            )
        with queue._transaction() as conn:
            conn.executemany(
                "INSERT INTO delivery_tombstones "
                "(execution_id, terminal_status, finished_at) VALUES (?, 'delivered', ?)",
                [(execution_id, "2026-09-26T00:00:01+00:00") for execution_id in ids],
            )

        original_connect = executions._connect
        ledger_connects = []

        def counted_connect():
            ledger_connects.append(True)
            return original_connect()

        monkeypatch.setattr(executions, "_connect", counted_connect)
        started = time.perf_counter()
        assert queue.reconcile_terminal_deliveries() == count
        elapsed = time.perf_counter() - started

        assert elapsed < 1.0
        assert queue.reconcile_terminal_deliveries() == 0
        assert len(ledger_connects) == 1
        with sqlite3.connect(delivery_db) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM delivery_tombstones WHERE projected=1"
            ).fetchone()[0] == count
    finally:
        reset_hermes_home_override(token)


def test_delivery_failure_is_terminal_not_retried_and_redacted(
    tmp_path, monkeypatch
):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-1", {"id": "job-1"}, "brief")
    send = Mock(return_value="request failed: https://example.test/?token=TOKEN123")

    assert queue.drain(send) == 1
    assert queue.drain(send) == 0
    assert send.call_count == 1
    status = queue.get_status("exec-1")
    assert status is not None
    assert status["status"] == "failed"
    assert "TOKEN123" not in status["error"]
    assert "token=***" in status["error"]


def test_wait_timeout_leaves_unclaimed_delivery_queued_for_next_gateway(
    tmp_path, monkeypatch
):
    """A row nobody claimed was never attempted: it is not uncertain, so a
    gateway outage longer than the worker's wait budget must not lose it."""
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    job = {"id": "job-3", "deliver": "origin"}

    error = queue.enqueue_and_wait("exec-3", job, "result", timeout=0)

    # Deferred, not failed: the worker must not record delivery_failed.
    assert error is None
    status = queue.get_status("exec-3")
    assert status is not None
    assert status["status"] == "pending"
    send = Mock(return_value=None)
    assert queue.drain(send) == 1
    send.assert_called_once_with(job, "result", False)
    assert queue.get_status("exec-3")["status"] == "delivered"


def test_same_gateway_recovers_terminalization_failure_without_resending(
    tmp_path, monkeypatch
):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-4", {"id": "job-4"}, "result")
    send = Mock(return_value=None)
    original_finish = queue._finish
    monkeypatch.setattr(
        queue,
        "_finish",
        Mock(side_effect=OSError("database temporarily unavailable")),
    )

    with pytest.raises(OSError, match="temporarily unavailable"):
        queue.drain(send)

    monkeypatch.setattr(queue, "_finish", original_finish)
    assert queue.drain(send) == 0
    send.assert_called_once()
    status = queue.get_status("exec-4")
    assert status["status"] == "unknown"
    assert "not retried" in status["error"]
