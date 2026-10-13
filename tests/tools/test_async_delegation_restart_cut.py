"""Regression coverage for restart-cut async delegation ledger truth and retention."""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    while True:
        try:
            process_registry.completion_queue.get_nowait()
        except queue.Empty:
            break
    yield
    deadline = time.monotonic() + 3.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while True:
        try:
            process_registry.completion_queue.get_nowait()
        except queue.Empty:
            break


def _event_for(delegation_id: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = process_registry.completion_queue.get(timeout=0.05)
        except queue.Empty:
            continue
        if event.get("delegation_id") == delegation_id:
            return event
    pytest.fail(f"no completion event for {delegation_id}")


def _cut_batch(results):
    started = threading.Event()
    release = threading.Event()

    def runner():
        started.set()
        assert release.wait(5)
        return {"results": results}

    handle = ad.dispatch_async_delegation_batch(
        goals=["one", "two", "three"], context="ctx", toolsets=None,
        role="leaf", model="m", session_key="session", parent_session_id="parent",
        runner=runner, interrupt_fn=lambda reason=None: release.set(), max_async_children=1,
    )
    assert handle["status"] == "dispatched"
    assert started.wait(5)
    assert ad.interrupt_all("gateway shutdown (post-interrupt)") == 1
    event = _event_for(handle["delegation_id"])
    return handle["delegation_id"], event


def test_shutdown_interrupt_persists_running_batch_as_interrupted():
    delegation_id, event = _cut_batch([
        {"task_index": 0, "status": "completed", "summary": "finished"},
        {"task_index": 1, "status": "interrupted", "error": "cut"},
        {"task_index": 2, "status": "interrupted", "error": "cut"},
    ])
    assert event["status"] == "completed"  # display status still reflects mixed child outcomes
    with ad._DB_LOCK, ad._transaction() as conn:
        state, event_json = conn.execute(
            "SELECT state, event_json FROM async_delegations WHERE delegation_id=?", (delegation_id,)
        ).fetchone()
    assert state == "interrupted"
    assert json.loads(event_json)["cut_reason"] == "gateway shutdown (post-interrupt)"


def test_shutdown_interrupt_persists_all_interrupted_batch_as_interrupted():
    delegation_id, event = _cut_batch([
        {"task_index": 0, "status": "interrupted", "error": "cut"},
        {"task_index": 1, "status": "interrupted", "error": "cut"},
        {"task_index": 2, "status": "interrupted", "error": "cut"},
    ])
    assert event["status"] == "error"  # the batch aggregate remains its historical display status
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute(
            "SELECT state FROM async_delegations WHERE delegation_id=?", (delegation_id,)
        ).fetchone() == ("interrupted",)


def test_prune_cap_ignores_exempt_rows_and_evicts_superseded_first():
    now = time.time()
    with ad._DB_LOCK, ad._transaction() as conn:
        for i in range(29):
            conn.execute(
                "INSERT INTO async_delegations "
                "(delegation_id, origin_session, parent_session_id, state, delivery_state, "
                "retry_state, retry_reason, dispatched_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"superseded-{i}", "s", "p", "error", "superseded", "none", "completed", now, now + i),
            )
        for i in range(22):
            conn.execute(
                "INSERT INTO async_delegations "
                "(delegation_id, origin_session, parent_session_id, state, delivery_state, "
                "dispatched_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"delivered-{i}", "s", "p", "completed", "delivered", now, now + i),
            )
        for i in range(18):
            conn.execute(
                "INSERT INTO async_delegations "
                "(delegation_id, origin_session, parent_session_id, state, delivery_state, "
                "dispatched_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"exempt-{i}", "s", "p", "unknown", "delivered", now, now + i),
            )
    ad._prune_durable_records()
    with ad._DB_LOCK, ad._transaction() as conn:
        remaining = {row[0] for row in conn.execute("SELECT delegation_id FROM async_delegations")}
    assert "superseded-0" not in remaining
    assert all(f"delivered-{i}" in remaining for i in range(22))
    assert all(f"exempt-{i}" in remaining for i in range(18))


def test_resumable_ceiling_never_deletes_retry_owed_rows():
    now = time.time()
    with ad._DB_LOCK, ad._transaction() as conn:
        for index in range(502):
            conn.execute(
                "INSERT INTO async_delegations "
                "(delegation_id, origin_session, parent_session_id, state, delivery_state, "
                "retry_state, dispatched_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"owed-{index}", "s", "p", "interrupted", "delivered", "scheduled", now, now + index),
            )
    ad._prune_durable_records()
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM async_delegations WHERE retry_state='scheduled'").fetchone() == (502,)
        assert conn.execute("SELECT 1 FROM async_delegations WHERE delegation_id='owed-0'").fetchone() == (1,)
        assert conn.execute("SELECT 1 FROM async_delegations WHERE delegation_id='owed-501'").fetchone() == (1,)


def test_restart_cut_row_survives_100_dispatches():
    delegation_id, _event = _cut_batch([
        {"task_index": 0, "status": "interrupted", "error": "cut"},
        {"task_index": 1, "status": "interrupted", "error": "cut"},
        {"task_index": 2, "status": "interrupted", "error": "cut"},
    ])
    for index in range(100):
        handle = ad.dispatch_async_delegation(
            goal=f"turnover-{index}", context=None, toolsets=None, role="leaf", model="m",
            session_key="session", runner=lambda: {"status": "completed", "summary": "ok"},
            max_async_children=1,
        )
        assert handle["status"] == "dispatched"
        _event_for(handle["delegation_id"])
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute(
            "SELECT state FROM async_delegations WHERE delegation_id=?", (delegation_id,)
        ).fetchone() == ("interrupted",)


@pytest.mark.parametrize("reason", ["gateway shutdown (post-interrupt)", "cli shutdown"])
def test_completed_single_runner_wins_over_interrupt_all(reason):
    started = threading.Event()
    release = threading.Event()

    def runner():
        started.set()
        assert release.wait(5)
        return {"status": "completed", "summary": "finished"}

    handle = ad.dispatch_async_delegation(
        goal="single", context="ctx", toolsets=None, role="leaf", model="m",
        session_key="session", parent_session_id="parent", runner=runner,
        interrupt_fn=lambda reason=None: release.set(), max_async_children=1,
    )
    assert handle["status"] == "dispatched"
    assert started.wait(5)
    assert ad.interrupt_all(reason) == 1
    event = _event_for(handle["delegation_id"])

    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute(
            "SELECT state FROM async_delegations WHERE delegation_id=?",
            (handle["delegation_id"],),
        ).fetchone() == ("completed",)
    from tools.delegation_resume import inspect_resumable
    record, reason = inspect_resumable(handle["delegation_id"])
    assert record is not None
    assert reason == "not_interrupted"
    assert event["status"] == "completed"
    assert "cut_reason" not in event


def _insert_history(did, state, delivery, updated):
    task = json.dumps({"goal": "single", "role": "leaf"})
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("""INSERT INTO async_delegations
            (delegation_id,origin_session,parent_session_id,state,delivery_state,
             dispatched_at,updated_at,task_json) VALUES (?, 's', 'p', ?, ?, ?, ?, ?)""",
            (did,state,delivery,updated,updated,task))


@pytest.mark.parametrize("protected_kind", ["pending", "retry_owed"])
def test_resumable_cap_keeps_delivery_and_retry_obligations(monkeypatch, protected_kind):
    now = time.time()
    _insert_history("owed", "interrupted", "pending" if protected_kind == "pending" else "delivered", now - 50)
    _insert_history("evictable", "interrupted", "delivered", now)
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET retry_state=? WHERE delegation_id='owed'",
                     ("scheduled" if protected_kind == "retry_owed" else "none",))
    monkeypatch.setattr(ad, "_MAX_RETAINED_RESUMABLE", 1)
    ad._prune_durable_records()
    with ad._DB_LOCK, ad._transaction() as conn:
        ids = {row[0] for row in conn.execute("SELECT delegation_id FROM async_delegations")}
    assert "owed" in ids
    assert "evictable" not in ids


@pytest.mark.parametrize("outbox", [False, True])
def test_completion_does_not_steal_boot_notice_claim(outbox):
    from tools import delegation_resume as dr
    did = "boot_claim"
    _insert_history(did, "interrupted", "pending", time.time())
    boot, reason = dr.claim_auto_resume_trigger(did)
    assert reason is None
    assert boot is not None
    if outbox:
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute("""INSERT INTO async_delegation_events
                (event_id,delegation_id,event_kind,event_json,created_at,updated_at)
                VALUES ('cut-event',?,'completion','{}',?,?)""", (did,time.time(),time.time()))
        claim = ad._claim_outbox_delivery("cut-event", "test")
    else:
        claim = ad.claim_event_delivery({"type": "async_delegation", "delegation_id": did}, "test")
    assert claim is not None
    assert ad.complete_completion_delivery(did, claim)
    assert dr.release_auto_resume_trigger(did, boot["auto_resume_claim"])
    again, reason = dr.claim_auto_resume_trigger(did)
    assert reason is None
    assert again is not None
    assert dr.complete_auto_resume_trigger(did, again["auto_resume_claim"])


def test_completion_ack_preserves_boot_claim_for_failed_admission():
    """A completion acknowledgment must not settle a still-owned boot notice claim."""
    from tools import delegation_resume as dr

    did = "boot_claim_ack"
    _insert_history(did, "interrupted", "pending", time.time())
    boot, reason = dr.claim_auto_resume_trigger(did)
    assert reason is None
    assert boot is not None

    assert ad.mark_completion_delivered(did)
    assert dr.release_auto_resume_trigger(did, boot["auto_resume_claim"])

    again, reason = dr.claim_auto_resume_trigger(did)
    assert reason is None
    assert again is not None
    assert dr.complete_auto_resume_trigger(did, again["auto_resume_claim"])


@pytest.mark.parametrize("pressure", ["resumable_cap", "expired"])
def test_retention_preserves_boot_claim_after_completion_ack(monkeypatch, pressure):
    """Retention cannot delete a boot notice while its owner can still release it."""
    from tools import delegation_resume as dr

    now = time.time()
    did = "boot_claim_pressure"
    _insert_history("pending_owed", "interrupted", "pending", now - 50)
    _insert_history(did, "interrupted", "pending", now)
    boot, reason = dr.claim_auto_resume_trigger(did)
    assert reason is None
    assert boot is not None
    assert ad.mark_completion_delivered(did)
    monkeypatch.setattr(ad, "_MAX_RETAINED_RESUMABLE", 1)
    expired = now - ad._DURABLE_RETENTION_SECONDS - 1
    if pressure == "expired":
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute("UPDATE async_delegations SET updated_at=? WHERE delegation_id=?", (expired, did))

    ad._prune_durable_records()
    assert dr.release_auto_resume_trigger(did, boot["auto_resume_claim"])
    again, reason = dr.claim_auto_resume_trigger(did)
    assert reason is None
    assert again is not None
    assert dr.complete_auto_resume_trigger(did, again["auto_resume_claim"])

    # Settling the claim makes completed history evictable again.
    if pressure == "expired":
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute("UPDATE async_delegations SET updated_at=? WHERE delegation_id=?", (expired, did))
    ad._prune_durable_records()
    with ad._DB_LOCK, ad._transaction() as conn:
        ids = {row[0] for row in conn.execute("SELECT delegation_id FROM async_delegations")}
    assert did not in ids
    assert "pending_owed" in ids
