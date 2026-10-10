"""Regression coverage for terminal-fallback ownership and ambiguous writes."""
import json
import queue
import sqlite3
import time
from contextlib import contextmanager

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry


def _record(delegation_id="deleg_race"):
    return {
        "delegation_id": delegation_id,
        "status": "running",
        "_durable_state": "running",
        "dispatched_at": time.time(),
        "completed_at": time.time(),
        "session_key": "synthetic-parent",
        "origin_ui_session_id": "ui",
        "parent_session_id": "root",
        "goal": "preserve the real result",
        "is_batch": True,
        "goals": ["first", "second"],
        "role": "leaf",
        "model": "synthetic",
    }


def _persist_running(record):
    ad._persist_dispatch(record)
    assert ad._persist_transition(record["delegation_id"], "queued", "admitted") == 1
    assert ad._persist_transition(record["delegation_id"], "admitted", "running") == 1


def _result(summary):
    return {
        "results": [
            {"task_index": 0, "status": "completed", "summary": summary},
            {"task_index": 1, "status": "error", "error": "synthetic child failure"},
        ],
        "live_transcripts": ["synthetic-transcript"],
        "group": "synthetic-group",
    }


def _drain_one(q):
    event = q.get_nowait()
    assert q.empty()
    return event


def test_commit_then_raise_does_not_create_a_redundant_terminal_fallback(tmp_path, monkeypatch):
    """A lifecycle commit followed by an ambiguous exception keeps one payload and identity."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record()
    _persist_running(record)
    result = _result("committed real result")
    original = ad._persist_completion

    def commit_then_raise(event, payload, **kwargs):
        original(event, payload, **kwargs)
        raise RuntimeError("synthetic commit-then-raise")

    monkeypatch.setattr(ad, "_persist_completion", commit_then_raise)
    monkeypatch.setattr(ad, "_records", {record["delegation_id"]: record})
    ad._push_completion_event(record, result, "completed")

    live = _drain_one(q)
    assert live["results"] == result["results"]
    assert "_delivery_event_id" not in live
    claim = ad.claim_event_delivery(live, "synthetic-parent")
    assert claim
    ad.complete_event_delivery(live, claim)
    replay = queue.Queue()
    assert ad.restore_undelivered_completions(replay) == 0
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM async_delegation_events").fetchone() == (0,)
        assert conn.execute(
            "SELECT state, result_json, delivery_state FROM async_delegations WHERE delegation_id=?",
            (record["delegation_id"],),
        ).fetchone()[0] == "completed"


def test_competing_terminal_write_does_not_enqueue_the_losing_payload(tmp_path, monkeypatch):
    """A zero-row terminal claim never sends a second completion beside the winner."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_competing")
    _persist_running(record)
    winner = _result("authoritative competing result")
    loser = _result("losing duplicate result")
    original = ad._persist_completion
    winner_event = {
        "type": "async_delegation", "delegation_id": record["delegation_id"],
        "session_key": record["session_key"], "status": "completed", "is_batch": True,
        "results": winner["results"], "live_transcripts": winner["live_transcripts"],
        "group": winner["group"],
    }
    original(winner_event, winner, expected_state="running")

    def competing_write(event, payload, **kwargs):
        return original(event, payload, **kwargs)

    monkeypatch.setattr(ad, "_persist_completion", competing_write)
    monkeypatch.setattr(ad, "_records", {record["delegation_id"]: record})
    ad._push_completion_event(record, loser, "completed")
    assert q.empty()

    replay = queue.Queue()
    assert ad.restore_undelivered_completions(replay) == 1
    replayed = _drain_one(replay)
    assert replayed["results"] == winner["results"]
    assert replayed["group"] == winner["group"]


@pytest.mark.parametrize("initial_write", ["raises", "zero_rows"])
def test_competitor_after_active_read_keeps_winner_replayable(tmp_path, monkeypatch, initial_write):
    """The winner commits between reconciliation and the fallback transaction."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_late_competitor")
    _persist_running(record)
    winner = _result("authoritative late winner")
    loser = _result("losing fallback")
    persist_completion = ad._persist_completion
    persist_outbox = ad._persist_outbox_event
    reconcile = ad._reconcile_terminal_write
    observations = []
    winner_event = {
        "type": "async_delegation", "delegation_id": record["delegation_id"],
        "session_key": record["session_key"], "status": "completed", "is_batch": True,
        "results": winner["results"], "live_transcripts": winner["live_transcripts"],
        "group": winner["group"],
    }

    def unavailable(*args, **kwargs):
        if initial_write == "zero_rows":
            return False
        raise RuntimeError("synthetic pre-commit write failure")

    def observe_reconciliation(*args, **kwargs):
        disposition = reconcile(*args, **kwargs)
        observations.append(disposition)
        if initial_write == "zero_rows" and observations == ["active"]:
            # The zero-row branch formerly queued directly after this read.
            assert persist_completion(winner_event, winner, expected_state="running")
        return disposition

    def commit_winner_before_fallback(event, payload, **kwargs):
        assert observations == ["active"]
        if initial_write == "raises":
            assert persist_completion(winner_event, winner, expected_state="running")
        return persist_outbox(event, payload, **kwargs)

    monkeypatch.setattr(ad, "_persist_completion", unavailable)
    monkeypatch.setattr(ad, "_reconcile_terminal_write", observe_reconciliation)
    monkeypatch.setattr(ad, "_persist_outbox_event", commit_winner_before_fallback)
    ad._push_completion_event(record, loser, "completed")
    with ad._DB_LOCK, ad._transaction() as conn:
        row = conn.execute(
            "SELECT result_json, delivery_state FROM async_delegations WHERE delegation_id=?",
            (record["delegation_id"],),
        ).fetchone()
        assert json.loads(row[0]) == winner
        assert row[1] == "pending"
        events = conn.execute("SELECT event_json FROM async_delegation_events").fetchall()
        assert events == []
    assert q.empty(), "losing payload must never be offered against the winner's delivery identity"

    replay = queue.Queue()
    assert ad.restore_undelivered_completions(replay) == 1
    replayed = _drain_one(replay)
    assert replayed["results"] == winner["results"]
    claim = ad.claim_event_delivery(replayed, "synthetic-parent")
    assert claim
    ad.complete_event_delivery(replayed, claim)
    assert ad.claim_event_delivery(replayed, "second-consumer") is None
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


def test_terminal_fallback_owns_active_row_and_replays_real_result_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_fallback")
    _persist_running(record)
    result = _result("fallback real result")
    monkeypatch.setattr(ad, "_persist_completion", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db unavailable")))
    monkeypatch.setattr(ad, "_records", {record["delegation_id"]: record})
    ad._push_completion_event(record, result, "completed")
    live = _drain_one(q)
    assert live["_delivery_event_id"] == f"{record['delegation_id']}:terminal_fallback"
    claim = ad.claim_event_delivery(live, "synthetic-parent")
    assert claim
    ad.complete_event_delivery(live, claim)
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


def test_fallback_commit_then_raise_preserves_the_committed_outbox_identity(tmp_path, monkeypatch):
    """A transaction can commit before its caller gets the event-id stamp."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_ambiguous_fallback")
    _persist_running(record)
    result = _result("committed fallback result")
    persist_outbox = ad._persist_outbox_event
    transaction = ad._transaction

    @contextmanager
    def commit_then_raise():
        with transaction() as conn:
            yield conn
        raise RuntimeError("synthetic post-commit transaction failure")

    def ambiguous_outbox(*args, **kwargs):
        with monkeypatch.context() as scoped:
            scoped.setattr(ad, "_transaction", commit_then_raise)
            return persist_outbox(*args, **kwargs)

    monkeypatch.setattr(ad, "_persist_completion", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("initial write failed")))
    monkeypatch.setattr(ad, "_persist_outbox_event", ambiguous_outbox)
    ad._push_completion_event(record, result, "completed")
    event = _drain_one(q)
    assert event["results"] == result["results"]
    assert event["_delivery_event_id"] == f"{record['delegation_id']}:terminal_fallback"
    claim = ad.claim_event_delivery(event, "synthetic-parent")
    assert claim and claim.startswith("outbox:")
    ad.complete_event_delivery(event, claim)
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


def _row(delegation_id):
    with ad._DB_LOCK, ad._transaction() as conn:
        return conn.execute(
            "SELECT state, delivery_state, result_json FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()


def _outbox_rows():
    with ad._DB_LOCK, ad._transaction() as conn:
        return conn.execute("SELECT event_id, delivery_state FROM async_delegation_events").fetchall()


def _winner_event(record, winner):
    return {
        "type": "async_delegation", "delegation_id": record["delegation_id"],
        "session_key": record["session_key"], "status": "completed", "is_batch": True,
        "results": winner["results"], "live_transcripts": winner["live_transcripts"],
        "group": winner["group"],
    }


def _reconcile_unavailable(*args, **kwargs):
    raise sqlite3.OperationalError("synthetic reconciliation outage")


def test_queued_cancel_commit_then_raise_delivers_its_own_lifecycle_once(tmp_path, monkeypatch):
    """The live status (interrupted) differs from the canonical persisted status (cancelled)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_queued_cancel")
    record.update(_durable_state="queued", _terminal_state="cancelled")
    ad._persist_dispatch(record)
    result = _result("cancelled owned result")
    original = ad._persist_completion

    def commit_then_raise(event, payload, **kwargs):
        original(event, payload, **kwargs)
        raise RuntimeError("synthetic commit-then-raise")

    monkeypatch.setattr(ad, "_persist_completion", commit_then_raise)
    ad._push_completion_event(record, result, "interrupted")
    live = _drain_one(q)
    assert live["status"] == "interrupted", "the intended live status is unchanged"
    assert "_delivery_event_id" not in live
    claim = ad.claim_event_delivery(live, "synthetic-parent")
    assert claim
    ad.complete_event_delivery(live, claim)
    assert _row(record["delegation_id"])[:2] == ("cancelled", "delivered")
    assert _outbox_rows() == []
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


def test_unavailable_reconciliation_never_claims_a_competing_winner(tmp_path, monkeypatch):
    """An unverified loser must not claim, by delegation_id, the row another writer owns."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_unverified_loser")
    _persist_running(record)
    winner, loser = _result("authoritative winner"), _result("uncertain loser")
    assert ad._persist_completion(_winner_event(record, winner), winner, expected_state="running")
    reconcile = ad._reconcile_terminal_write
    monkeypatch.setattr(ad, "_reconcile_terminal_write", _reconcile_unavailable)
    ad._push_completion_event(record, loser, "completed")

    queued = _drain_one(q)
    assert ad.claim_event_delivery(queued, "synthetic-parent") is None, "ownership is still unproven"
    assert _row(record["delegation_id"])[1] == "pending"
    assert q.empty()
    # Still unresolved: the copy is held for a later offer, neither lost nor delivered.
    assert ad.reoffer_unresolved_completions(q, now=time.time() + 3600) == 1
    retry = _drain_one(q)
    assert retry["results"] == loser["results"]

    monkeypatch.setattr(ad, "_reconcile_terminal_write", reconcile)
    assert ad.claim_event_delivery(retry, "synthetic-parent") is None, "the recovered check proves it lost"
    assert ad.reoffer_unresolved_completions(q, now=time.time() + 3600) == 0
    assert q.empty(), "a proven loser is never offered again"
    assert _row(record["delegation_id"])[1] == "pending"
    replay = queue.Queue()
    assert ad.restore_undelivered_completions(replay) == 1
    assert _drain_one(replay)["results"] == winner["results"]


def test_unavailable_reconciliation_after_primary_commit_delivers_once_after_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_unverified_owner")
    _persist_running(record)
    result = _result("owned result behind an outage")
    original = ad._persist_completion
    reconcile = ad._reconcile_terminal_write

    def commit_then_raise(event, payload, **kwargs):
        original(event, payload, **kwargs)
        raise RuntimeError("synthetic commit-then-raise")

    monkeypatch.setattr(ad, "_persist_completion", commit_then_raise)
    monkeypatch.setattr(ad, "_reconcile_terminal_write", _reconcile_unavailable)
    ad._push_completion_event(record, result, "completed")
    event = _drain_one(q)
    assert ad.claim_event_delivery(event, "synthetic-parent") is None
    assert _row(record["delegation_id"])[:2] == ("completed", "pending")
    assert ad.reoffer_unresolved_completions(q, now=time.time()) == 0, "held behind its retry backoff"
    assert ad.reoffer_unresolved_completions(q, now=time.time() + 3600) == 1
    event = _drain_one(q)
    monkeypatch.setattr(ad, "_reconcile_terminal_write", reconcile)
    claim = ad.claim_event_delivery(event, "synthetic-parent")
    assert claim and not claim.startswith("outbox:")
    ad.complete_event_delivery(event, claim)
    assert _row(record["delegation_id"])[:2] == ("completed", "delivered")
    assert ad.claim_event_delivery(dict(event), "second-consumer") is None
    assert q.empty()
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


def test_fallback_commit_then_raise_with_unavailable_reconciliation_settles_its_outbox(tmp_path, monkeypatch):
    """Acknowledging the live copy must settle the committed outbox row, not only the lifecycle row."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_unverified_fallback")
    _persist_running(record)
    result = _result("committed fallback behind an outage")
    persist_outbox = ad._persist_outbox_event
    reconcile = ad._reconcile_terminal_write
    calls = []

    def commit_then_raise(*args, **kwargs):
        persist_outbox(*args, **kwargs)
        args[0].pop("_delivery_event_id", None)  # the stamp never reached the caller
        raise RuntimeError("synthetic post-commit fallback failure")

    def active_then_unavailable(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return reconcile(*args, **kwargs)
        raise sqlite3.OperationalError("synthetic reconciliation outage")

    monkeypatch.setattr(ad, "_persist_completion", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("initial write failed")))
    monkeypatch.setattr(ad, "_persist_outbox_event", commit_then_raise)
    monkeypatch.setattr(ad, "_reconcile_terminal_write", active_then_unavailable)
    ad._push_completion_event(record, result, "completed")
    event = _drain_one(q)
    assert "_delivery_event_id" not in event
    monkeypatch.setattr(ad, "_reconcile_terminal_write", reconcile)
    claim = ad.claim_event_delivery(event, "synthetic-parent")
    assert claim and claim.startswith("outbox:")
    ad.complete_event_delivery(event, claim)
    assert _outbox_rows() == [(f"{record['delegation_id']}:terminal_fallback", "delivered")]
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


def test_unverified_active_row_acquires_fallback_ownership_at_claim(tmp_path, monkeypatch):
    """Nothing committed during the outage: the claim acquires the row before delivering."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_unverified_active")
    _persist_running(record)
    result = _result("result that never committed")
    reconcile = ad._reconcile_terminal_write
    monkeypatch.setattr(ad, "_persist_completion", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("write failed")))
    monkeypatch.setattr(ad, "_reconcile_terminal_write", _reconcile_unavailable)
    ad._push_completion_event(record, result, "completed")
    event = _drain_one(q)
    assert _row(record["delegation_id"])[0] == "running"
    monkeypatch.setattr(ad, "_reconcile_terminal_write", reconcile)
    claim = ad.claim_event_delivery(event, "synthetic-parent")
    assert claim and claim.startswith("outbox:")
    ad.complete_event_delivery(event, claim)
    assert _row(record["delegation_id"])[0] == "completed"
    assert _outbox_rows() == [(f"{record['delegation_id']}:terminal_fallback", "delivered")]
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


def test_unavailable_database_holds_the_result_until_ownership_is_proven(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_unavailable")
    _persist_running(record)
    result = _result("real live result")
    transaction = ad._transaction

    @contextmanager
    def unavailable():
        raise sqlite3.OperationalError("synthetic database outage")
        yield  # pragma: no cover — contextmanager's generator protocol

    monkeypatch.setattr(ad, "_transaction", unavailable)
    ad._push_completion_event(record, result, "completed")
    event = _drain_one(q)
    assert "_delivery_event_id" not in event
    assert ad.claim_event_delivery(event, "synthetic-parent") is None
    assert q.empty()
    assert ad.reoffer_unresolved_completions(q, now=time.time() + 3600) == 1
    event = _drain_one(q)
    assert event["results"] == result["results"], "the real result is held while unproven"
    monkeypatch.setattr(ad, "_transaction", transaction)
    claim = ad.claim_event_delivery(event, "synthetic-parent")
    assert claim and claim.startswith("outbox:")
    ad.complete_event_delivery(event, claim)
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


def test_missing_lifecycle_row_keeps_result_in_memory_without_phantom_outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_missing")
    result = _result("unowned real result")
    monkeypatch.setattr(ad, "_persist_completion", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db unavailable")))
    monkeypatch.setattr(ad, "_records", {record["delegation_id"]: record})
    ad._push_completion_event(record, result, "completed")
    event = _drain_one(q)
    assert event["results"] == result["results"]
    assert "_delivery_event_id" not in event
    # Proven absent: no durable identity exists, so delivery stays in memory only.
    assert ad.claim_event_delivery(event, "synthetic-parent")
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM async_delegation_events").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM async_delegations").fetchone() == (0,)


def _unproven_loser(tmp_path, monkeypatch, q):
    """A competing winner owns the row; this writer's reconciliation is unavailable."""
    monkeypatch.setattr(ad, "_unresolved", {})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_consumer_loser")
    _persist_running(record)
    winner, loser = _result("consumer winner"), _result("consumer loser")
    assert ad._persist_completion(_winner_event(record, winner), winner, expected_state="running")
    monkeypatch.setattr(ad, "_reconcile_terminal_write", _reconcile_unavailable)
    ad._push_completion_event(record, loser, "completed")
    return record, winner


def test_tui_poller_never_shows_an_unproven_terminal_result(tmp_path, monkeypatch):
    import threading
    from tools.process_registry_notifications import format_process_notification
    from tui_gateway import server

    q = queue.Queue()
    record, _winner = _unproven_loser(tmp_path, monkeypatch, q)
    event = _drain_one(q)
    emitted, dispatched = [], []
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_notif_dispatch_event", lambda *args: dispatched.append(args))
    session = {"session_key": event["session_key"], "history_lock": threading.RLock()}
    registry = type("Registry", (), {"completion_queue": q})()
    assert server._notif_handle_event("ui", session, event, set(), registry, format_process_notification, None)
    assert emitted == [] and dispatched == []
    assert q.empty()
    assert len(ad._unresolved) == 1, "held, not lost"
    assert _row(record["delegation_id"])[1] == "pending"


def test_cli_drain_never_injects_an_unproven_terminal_result(tmp_path, monkeypatch):
    from cli import HermesCLI

    q = queue.Queue()
    record, winner = _unproven_loser(tmp_path, monkeypatch, q)
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "synthetic-parent"
    cli._pending_input = queue.Queue()
    monkeypatch.setattr(cli, "_owns_process_notification", lambda evt: True, raising=False)
    monkeypatch.setattr(process_registry, "restore_completions", lambda: 0)
    cli._drain_process_notifications("cli-idle")
    assert cli._pending_input.empty()
    assert len(ad._unresolved) == 1, "held, not lost"
    assert _row(record["delegation_id"])[1] == "pending", "the winner's row is not consumed"
    replay = queue.Queue()
    assert ad.restore_undelivered_completions(replay) == 1
    assert _drain_one(replay)["results"] == winner["results"]


def test_gateway_watcher_never_injects_an_unproven_terminal_result(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from collections import OrderedDict

    q = queue.Queue()
    record, _winner = _unproven_loser(tmp_path, monkeypatch, q)
    monkeypatch.setattr(ad, "_owner_liveness", lambda: None)
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.session_store = SimpleNamespace(_ensure_loaded=lambda: None, _entries={})
    runner._session_source_cache = {}
    runner._completion_delivery_lock = __import__("threading").Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 2048
    runner._background_tasks = set()
    runner._completion_notification_batch_window = 0
    event = q.queue[0]
    event["session_key"] = "agent:main:telegram:dm:12345:678"
    event.pop("parent_session_id", None)  # no /new-boundary lookup: reach the durable claim
    sleeps = []

    async def _bounded_sleep(_delay):
        sleeps.append(1)
        if len(sleeps) >= 2:
            runner._running = False

    monkeypatch.setattr(asyncio, "sleep", _bounded_sleep)
    asyncio.run(runner._async_delegation_watcher(interval=0))
    adapter.handle_message.assert_not_awaited()
    assert q.empty()
    assert list(ad._unresolved.values())[0]["results"] == _result("consumer loser")["results"], "held, not lost"
    assert _row(record["delegation_id"])[1] == "pending"
