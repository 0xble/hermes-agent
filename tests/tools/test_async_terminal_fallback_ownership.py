"""Regression coverage for terminal-fallback ownership and ambiguous writes."""
import json
import queue
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


def test_unavailable_fallback_preserves_live_result_without_outbox_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_unavailable")
    _persist_running(record)
    result = _result("real live result")

    @contextmanager
    def unavailable():
        raise RuntimeError("synthetic database outage")
        yield  # pragma: no cover — contextmanager's generator protocol

    monkeypatch.setattr(ad, "_transaction", unavailable)
    ad._push_completion_event(record, result, "completed")
    event = _drain_one(q)
    assert event["results"] == result["results"]
    assert "_delivery_event_id" not in event


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
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM async_delegation_events").fetchone() == (0,)
