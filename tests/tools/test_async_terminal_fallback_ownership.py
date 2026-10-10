"""Regression coverage for terminal-fallback ownership and ambiguous writes."""
import queue
import time

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
    ad._persist_dispatch(record)
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
    ad._persist_dispatch(record)
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


def test_terminal_fallback_owns_active_row_and_replays_real_result_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    record = _record("deleg_fallback")
    ad._persist_dispatch(record)
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
