"""Tests for async (background) delegation — tools/async_delegation.py.

Covers the dispatch handle, non-blocking behavior, completion-event delivery
onto the shared process_registry.completion_queue, the rich re-injection block
formatting, capacity rejection, and crash handling.
"""

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry
from tools.process_registry_notifications import format_process_notification


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    # Give just-released workers a beat to finalize BEFORE draining, so their
    # completion events land now instead of leaking into the next test's
    # queue (worker threads push events asynchronously; a drain that races an
    # in-flight _finalize misses it).
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _drain_one(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


def _drain_for(delegation_id, timeout=5.0):
    """Drain until the event for *delegation_id* appears (discarding others).

    Completion events are pushed asynchronously by worker threads, so a
    straggler from a PREVIOUS test can land after that test's teardown drain
    and leak into the current test's queue. Matching on delegation_id makes
    the assertion immune to that cross-test leak.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            evt = process_registry.completion_queue.get_nowait()
            if evt.get("delegation_id") == delegation_id:
                return evt
            continue
        time.sleep(0.02)
    return None


def _wait_for_queued(delegation_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = next((item for item in ad.list_async_delegations()
                     if item["delegation_id"] == delegation_id), None)
        if item is not None and item["status"] == "queued":
            return
        time.sleep(0.01)
    pytest.fail(f"delegation {delegation_id} did not remain queued")


def test_schema_init_preserves_shared_state_db_journal_mode(tmp_path):
    """The delegation ledger is a guest in state.db, not its mode owner."""
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"

        ad._initialize_schema(conn)

        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='async_delegations'"
        ).fetchone() == ("async_delegations",)
    finally:
        conn.close()


def test_schema_init_preserves_shared_state_db_wal_mode(tmp_path):
    """Schema initialization must not replace an existing WAL mode."""
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"

        ad._initialize_schema(conn)

        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='async_delegations'"
        ).fetchone() == ("async_delegations",)
    finally:
        conn.close()


@pytest.mark.platforms("macos")
def test_connect_preserves_wal_and_applies_macos_durability_barriers(
    tmp_path, monkeypatch
):
    """Each ledger connection must carry the macOS write barriers."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    seed = sqlite3.connect(tmp_path / "state.db")
    try:
        assert seed.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    finally:
        seed.close()

    conn = ad._connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1
    finally:
        conn.close()




def test_async_executor_workers_are_daemon_threads():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "done"}

    res = ad.dispatch_async_delegation(
        goal="daemon check", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=1,
    )
    assert res["status"] == "dispatched"

    deadline = time.monotonic() + 2
    worker = None
    while time.monotonic() < deadline:
        worker = next(
            (t for t in threading.enumerate() if t.name.startswith("async-delegate")),
            None,
        )
        if worker is not None:
            break
        time.sleep(0.02)
    assert worker is not None
    assert worker.daemon is True
    gate.set()
    assert _drain_one() is not None


def test_completion_event_lands_on_shared_queue_with_session_key():
    def runner():
        return {"status": "completed", "summary": "the result",
                "api_calls": 3, "duration_seconds": 2.0, "model": "test-model"}

    res = ad.dispatch_async_delegation(
        goal="compute X", context="some context", toolsets=["web", "file"],
        role="leaf", model="test-model", session_key="agent:main:cli:dm:local",
        parent_session_id="20260703_parent_sid",
        runner=runner, max_async_children=3,
    )
    assert res["status"] == "dispatched"

    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["summary"] == "the result"
    assert evt["session_key"] == "agent:main:cli:dm:local"
    assert evt["parent_session_id"] == "20260703_parent_sid"
    assert evt["delegation_id"] == res["delegation_id"]


def test_rich_reinjection_block_is_self_contained():
    def runner():
        return {"status": "completed", "summary": "The answer is 42.",
                "api_calls": 7, "duration_seconds": 3.5, "model": "test-model"}

    ad.dispatch_async_delegation(
        goal="Compute the meaning of life",
        context="User is a philosopher. Respond tersely.",
        toolsets=["web"], role="leaf", model="test-model",
        session_key="", runner=runner, max_async_children=3,
    )
    evt = _drain_one()
    assert evt is not None
    text = format_process_notification(evt)
    assert text is not None
    for needle in [
        "Compute the meaning of life",
        "User is a philosopher",
        "The answer is 42.",
    ]:
        assert needle in text, f"missing {needle!r}"


def test_dispatch_queues_at_capacity_and_admits_after_slot_release():
    ev = threading.Event()
    started = threading.Event()

    def blocker():
        ev.wait(timeout=60)
        return {"status": "completed", "summary": "x"}

    first = ad.dispatch_async_delegation(
        goal="task0", context=None, toolsets=None, role="leaf", model="m", session_key="owned",
        runner=blocker, max_async_children=1,
    )
    queued = ad.dispatch_async_delegation(
        goal="task1", context=None, toolsets=None, role="leaf", model="m", session_key="owned",
        runner=lambda: (started.set(), {"status": "completed", "summary": "queued result"})[1],
        max_async_children=1, max_queued_delegations=1,
    )
    assert first["status"] == "dispatched"
    assert queued["status"] == "queued"
    assert any(r["delegation_id"] == queued["delegation_id"] and r["status"] == "queued"
               for r in ad.list_async_delegations())
    assert not started.wait(0.1)

    ev.set()
    assert _drain_for(first["delegation_id"]) is not None
    assert started.wait(5)
    assert _drain_for(queued["delegation_id"]) is not None


def test_queued_delegation_is_visible_to_action_list_and_routes_to_owner():
    from tools.delegate_tool_registry import _list_payload

    release = threading.Event()
    parent = type("Parent", (), {"session_id": "list-owner"})()
    first = ad.dispatch_async_delegation(
        goal="occupy", context=None, toolsets=None, role="leaf", model="m",
        session_key="owner", parent_session_id="other-owner",
        runner=lambda: (release.wait(30), {"status": "completed"})[1], max_async_children=1,
    )
    queued = ad.dispatch_async_delegation(
        goal="visible queued goal", context=None, toolsets=None, role="leaf", model="m",
        session_key="owner", parent_session_id="list-owner",
        runner=lambda: {"status": "completed"}, max_async_children=1, max_queued_delegations=1,
    )
    try:
        assert first["status"] == "dispatched"
        assert queued["status"] == "queued"
        payload = _list_payload(parent)
        entry = next(item for item in payload["subagents"] if item["delegation_id"] == queued["delegation_id"])
        assert entry["status"] == "queued"
        assert entry["goal"] == "visible queued goal"
        assert entry["queue_reason"] == "async pool capacity"
    finally:
        assert ad.interrupt_delegation(queued["delegation_id"], reason="test cleanup")
        release.set()
        assert _drain_for(first["delegation_id"]) is not None


def test_queued_delegation_controls_follow_compression_lineage(tmp_path):
    """A rotated parent must still list and cancel queued work before admission."""
    from hermes_state import SessionDB
    from tools.delegate_tool_registry import _handle_control_action, _list_payload

    release = threading.Event()
    started = threading.Event()
    db = SessionDB(db_path=tmp_path / "lineage.db")
    db.create_session(session_id="parent-old", source="tui", model="test")
    db.append_message(session_id="parent-old", role="user", content="before compression")
    db.end_session("parent-old", end_reason="compression")
    db.create_session(session_id="parent-new", source="tui", model="test", parent_session_id="parent-old")
    db.append_message(session_id="parent-new", role="user", content="after compression")
    parent = SimpleNamespace(session_id="parent-new", _session_db=db)
    first = ad.dispatch_async_delegation(
        goal="occupy", context=None, toolsets=None, role="leaf", model="m",
        session_key="owner", parent_session_id="parent-old",
        runner=lambda: (release.wait(30), {"status": "completed"})[1], max_async_children=1,
    )
    queued = ad.dispatch_async_delegation(
        goal="cancel after rotation", context=None, toolsets=None, role="leaf", model="m",
        session_key="owner", parent_session_id="parent-old",
        runner=lambda: (started.set(), {"status": "completed"})[1],
        max_async_children=1, max_queued_delegations=1,
    )
    try:
        assert first["status"] == "dispatched"
        assert queued["status"] == "queued"
        assert db.resolve_resume_session_id("parent-old") == "parent-new"
        payload = _list_payload(parent)
        assert any(item["delegation_id"] == queued["delegation_id"] for item in payload["subagents"])
        stopped = json.loads(_handle_control_action("stop", queued["delegation_id"], None, parent))
        assert stopped["status"] == "interrupt_requested"
        assert not started.wait(0.2)
        completion = _drain_for(queued["delegation_id"])
        assert completion is not None
        assert completion["status"] == "interrupted"
    finally:
        release.set()
        assert _drain_for(first["delegation_id"]) is not None
        db.close()


def test_pending_queue_overflow_rejects_without_starting():
    ev = threading.Event()
    started = threading.Event()
    blocker = lambda: (ev.wait(60), {"status": "completed"})[1]
    first = ad.dispatch_async_delegation(
        goal="task0", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=blocker, max_async_children=1, max_queued_delegations=1,
    )
    second = ad.dispatch_async_delegation(
        goal="task1", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=blocker, max_async_children=1, max_queued_delegations=1,
    )
    overflow = ad.dispatch_async_delegation(
        goal="task2", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: (started.set(), {})[1], max_async_children=1, max_queued_delegations=1,
    )
    assert first["status"] == "dispatched"
    assert second["status"] == "queued"
    assert overflow["status"] == "rejected"
    assert overflow["at_capacity"] is True
    assert overflow["queue_full"] is True
    assert not started.wait(0.1)
    ev.set()
    assert _drain_for(first["delegation_id"]) is not None
    assert _drain_for(second["delegation_id"]) is not None


def test_queued_siblings_share_one_bounded_slot_and_admit_together():
    """Independent completion units from one call share one pending-slot reservation."""
    release = threading.Event()
    first = ad.dispatch_async_delegation(
        goal="occupy", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: (release.wait(30), {"status": "completed"})[1],
        max_async_children=1, max_queued_delegations=1,
    )
    started = [threading.Event(), threading.Event()]
    slot_key = "one-call-slot"
    units = [
        ad.dispatch_async_delegation_batch(
            goals=[f"unit-{i}"], context=None, toolsets=None, role="leaf", model="m", session_key="",
            slot_key=slot_key,
            runner=lambda i=i: (started[i].set(), {"results": [{"task_index": 0, "status": "completed"}]})[1],
            max_async_children=1, max_queued_delegations=1,
        )
        for i in range(2)
    ]
    assert first["status"] == "dispatched"
    assert [unit["status"] for unit in units] == ["queued", "queued"]
    with ad._records_lock:
        assert ad._queued_count_locked() == 1
    release.set()
    assert _drain_for(first["delegation_id"]) is not None
    assert all(event.wait(5) for event in started)
    assert all(_drain_for(unit["delegation_id"]) is not None for unit in units)


def test_queued_worker_preserves_parent_prompt_callbacks(monkeypatch):
    """A queued worker inherits the dispatching thread's approval callback."""
    from tools import thread_context

    class CallbackSlot:
        value: object | None = None

    slot = CallbackSlot()
    callback = object()
    monkeypatch.setattr(thread_context, "_callback_api", lambda: ((lambda: slot.value, lambda value: setattr(slot, "value", value)),))
    slot.value = callback
    release = threading.Event()
    first = ad.dispatch_async_delegation(
        goal="occupy", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: (release.wait(30), {"status": "completed"})[1], max_async_children=1,
    )
    observed = []
    queued = ad.dispatch_async_delegation(
        goal="callback", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: (observed.append(slot.value), {"status": "completed"})[1],
        max_async_children=1, max_queued_delegations=1,
    )
    assert queued["status"] == "queued"
    release.set()
    assert _drain_for(first["delegation_id"]) is not None
    assert _drain_for(queued["delegation_id"]) is not None
    assert observed == [callback]


def test_durable_prune_never_evicts_queued_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    now = time.time()
    with ad._DB_LOCK, ad._transaction() as conn:
        for i in range(ad._MAX_RETAINED_COMPLETED + 1):
            conn.execute(
                "INSERT INTO async_delegations (delegation_id, origin_session, state, dispatched_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (f"done-{i}", "test", "completed", now, now),
            )
        conn.execute(
            "INSERT INTO async_delegations (delegation_id, origin_session, state, dispatched_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("queued-row", "test", "queued", now, now),
        )
    ad._prune_durable_records()
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute(
            "SELECT state FROM async_delegations WHERE delegation_id='queued-row'"
        ).fetchone() == ("queued",)


def test_queued_cancel_claims_before_admission_can_race(monkeypatch):
    """Queued cancellation removes the row before callbacks or slot release can admit it."""
    release = threading.Event()
    first = ad.dispatch_async_delegation(
        goal="occupy", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: (release.wait(30), {"status": "completed"})[1], max_async_children=1,
    )
    started = threading.Event()
    queued = ad.dispatch_async_delegation(
        goal="cancel", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: (started.set(), {"status": "completed"})[1],
        max_async_children=1, max_queued_delegations=1,
    )
    callback_entered = threading.Event()
    allow_callback = threading.Event()
    original = ad._call_interrupt

    def pause_callback(*args, **kwargs):
        callback_entered.set()
        assert allow_callback.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(ad, "_call_interrupt", pause_callback)
    stopper = threading.Thread(target=lambda: ad.interrupt_delegation(queued["delegation_id"], reason="race"))
    stopper.start()
    assert callback_entered.wait(5)
    release.set()
    assert _drain_for(first["delegation_id"]) is not None
    assert not started.is_set()
    allow_callback.set()
    stopper.join(timeout=5)
    assert not stopper.is_alive()
    completion = _drain_for(queued["delegation_id"])
    assert completion is not None
    assert completion["status"] == "interrupted"


def test_queued_delegation_can_be_cancelled_before_runner_starts():
    ev = threading.Event()
    started = threading.Event()
    interrupted = []
    first = ad.dispatch_async_delegation(
        goal="task0", context=None, toolsets=None, role="leaf", model="m", session_key="owned",
        runner=lambda: (ev.wait(60), {"status": "completed"})[1], max_async_children=1,
    )
    queued = ad.dispatch_async_delegation(
        goal="task1", context=None, toolsets=None, role="leaf", model="m", session_key="owned",
        runner=lambda: (started.set(), {})[1], interrupt_fn=lambda reason=None: interrupted.append(reason),
        max_async_children=1, max_queued_delegations=1,
    )
    assert queued["status"] == "queued"
    assert ad.interrupt_delegation(queued["delegation_id"], reason="stop")
    event = _drain_for(queued["delegation_id"])
    assert event is not None
    assert event["status"] == "interrupted"
    assert not started.is_set()
    assert interrupted == ["stop"]
    ev.set()
    assert _drain_for(first["delegation_id"]) is not None


def test_queued_admission_does_not_submit_when_ledger_write_raises(monkeypatch):
    from contextlib import contextmanager

    release = threading.Event()
    started = threading.Event()
    first = ad.dispatch_async_delegation(
        goal="occupy", context=None, toolsets=None, role="leaf", model="m", session_key="owned",
        runner=lambda: (release.wait(30), {"status": "completed"})[1], max_async_children=1,
    )
    queued = ad.dispatch_async_delegation(
        goal="queued", context=None, toolsets=None, role="leaf", model="m", session_key="owned",
        runner=lambda: (started.set(), {"status": "completed"})[1],
        max_async_children=1, max_queued_delegations=1,
    )
    assert queued["status"] == "queued"
    original_transaction = ad._transaction

    class RaisingConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if "SET state='running'" in sql:
                raise RuntimeError("admission write failed")
            return self._conn.execute(sql, params)

    @contextmanager
    def transaction():
        with original_transaction() as conn:
            yield RaisingConnection(conn)

    monkeypatch.setattr(ad, "_transaction", transaction)
    release.set()
    assert _drain_for(first["delegation_id"]) is not None
    assert not started.is_set()
    _wait_for_queued(queued["delegation_id"])
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute(
            "SELECT state FROM async_delegations WHERE delegation_id=?", (queued["delegation_id"],)
        ).fetchone() == ("queued",)


def test_queued_admission_does_not_submit_when_ledger_write_changes_zero_rows(monkeypatch):
    from contextlib import contextmanager

    release = threading.Event()
    started = threading.Event()
    first = ad.dispatch_async_delegation(
        goal="occupy", context=None, toolsets=None, role="leaf", model="m", session_key="owned",
        runner=lambda: (release.wait(30), {"status": "completed"})[1], max_async_children=1,
    )
    queued = ad.dispatch_async_delegation(
        goal="queued", context=None, toolsets=None, role="leaf", model="m", session_key="owned",
        runner=lambda: (started.set(), {"status": "completed"})[1],
        max_async_children=1, max_queued_delegations=1,
    )
    assert queued["status"] == "queued"
    original_transaction = ad._transaction

    class ZeroRowsCursor:
        rowcount = 0

    class ZeroRowsConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if "SET state='running'" in sql:
                return ZeroRowsCursor()
            return self._conn.execute(sql, params)

    @contextmanager
    def transaction():
        with original_transaction() as conn:
            yield ZeroRowsConnection(conn)

    monkeypatch.setattr(ad, "_transaction", transaction)
    release.set()
    assert _drain_for(first["delegation_id"]) is not None
    assert not started.is_set()
    _wait_for_queued(queued["delegation_id"])
    with ad._DB_LOCK, ad._transaction() as conn:
        assert conn.execute(
            "SELECT state FROM async_delegations WHERE delegation_id=?", (queued["delegation_id"],)
        ).fetchone() == ("queued",)


def test_interrupt_all_signals_running_children():
    ev = threading.Event()
    interrupted = {"count": 0}
    # No short internal timeout: the blocker holds until interrupt_fn fires.
    # The old ev.wait(timeout=5) made this test a change-detector for CI
    # worker load — on a CPU-starved runner the 5s expired before
    # interrupt_all() ran, the record finalized, and interrupt_all() found
    # nothing running (n == 0). The pytest-level timeout is the real
    # runaway guard.

    def blocker():
        ev.wait(timeout=60)
        return {"status": "interrupted", "summary": None,
                "error": "cancelled"}

    def interrupt_fn():
        interrupted["count"] += 1
        ev.set()

    r = ad.dispatch_async_delegation(
        goal="long task", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=blocker,
        interrupt_fn=interrupt_fn, max_async_children=3,
    )
    n = ad.interrupt_all(reason="test")
    assert n == 1
    assert interrupted["count"] == 1
    # child still emits a completion event after interrupt. Match on THIS
    # delegation's id — straggler 'completed' events from a previous test's
    # workers can finalize after that test's teardown drain and leak into
    # this queue (observed on loaded CI workers).
    evt = _drain_for(r["delegation_id"])
    assert evt is not None
    assert evt["status"] == "interrupted"


def _fast_stale_monitor(monkeypatch, *, idle=0.15, in_tool=0.3, grace=0.15):
    """Shrink the stale-monitor cadence so tests run in milliseconds."""
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.03)
    monkeypatch.setattr(ad, "_STALE_IDLE_SECONDS", idle)
    monkeypatch.setattr(ad, "_STALE_IN_TOOL_SECONDS", in_tool)
    monkeypatch.setattr(ad, "_STALL_GRACE_SECONDS", grace)


def test_stalled_runner_is_interrupted_then_finalized(monkeypatch):
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    interrupted = {"count": 0}

    def stuck_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "too late"}

    def interrupt_fn():
        interrupted["count"] += 1

    res = ad.dispatch_async_delegation(
        goal="stuck child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=stuck_runner,
        interrupt_fn=interrupt_fn, max_async_children=1,
        # Frozen progress token: the child never advances an API call.
        progress_fn=lambda: ((0, None), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["type"] == "async_delegation"
        assert evt["status"] == "stalled"
        assert evt["delegation_id"] == res["delegation_id"]
        assert evt["api_calls"] == 0
        assert "stopped responding" in evt["error"]  # status carries "stalled"; the text is for the user
        # Interrupt was requested BEFORE force-finalization (grace window).
        assert interrupted["count"] >= 1
        assert ad.active_count() == 0
    finally:
        gate.set()

    # If the ignored runner eventually returns, it must not enqueue a second
    # completion for a delegation the monitor already finalized.
    assert _drain_one(timeout=0.5) is None


def test_progressing_runner_is_never_stalled(monkeypatch):
    """A child that keeps advancing is left alone no matter how long it runs."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    ticks = {"n": 0}

    def slow_but_alive_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "done", "api_calls": 7}

    def progress_fn():
        # Token advances on every sample — simulates a child making steady
        # API-call progress.
        ticks["n"] += 1
        return (ticks["n"], None), False

    res = ad.dispatch_async_delegation(
        goal="slow child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=slow_but_alive_runner,
        max_async_children=1, progress_fn=progress_fn,
    )
    assert res["status"] == "dispatched"

    # Run well past the (shrunk) idle threshold — several monitor sweeps.
    time.sleep(0.6)
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"
    assert evt["summary"] == "done"


def test_stalling_runner_that_honors_interrupt_keeps_its_result(monkeypatch):
    """Interrupt-responsive children finalize through the NORMAL path.

    The monitor's interrupt gives a wedged-looking child a grace window; if
    the runner returns during it, the real result (partial work, api_calls)
    is delivered instead of a synthetic stalled event.
    """
    _fast_stale_monitor(monkeypatch, grace=5.0)
    interrupted = threading.Event()
    interrupt_reason = {}

    def interrupt_fn(reason=None):
        interrupt_reason["value"] = reason
        interrupted.set()

    def runner():
        # "Wedged" until interrupted, then unwinds and reports partial work.
        interrupted.wait(timeout=10)
        return {
            "status": "interrupted",
            "summary": "partial work saved",
            "api_calls": 3,
        }

    res = ad.dispatch_async_delegation(
        goal="responsive child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=runner,
        interrupt_fn=interrupt_fn, max_async_children=1,
        progress_fn=lambda: ((3, None), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "interrupted"
    assert evt["summary"] == "partial work saved"
    assert evt["api_calls"] == 3
    reason = interrupt_reason["value"]
    assert reason.startswith("stalled: no progress for ")
    assert reason.endswith("s")
    assert reason[len("stalled: no progress for "):-1].isdigit()
    assert ad.active_count() == 0


def test_streaming_child_counts_as_alive(monkeypatch):
    """A child mid-stream (api_call_count frozen, last_activity_ts ticking)
    must never be stalled — streamed chunks tick _touch_activity, and the
    progress token includes that timestamp (same liveness signal as the
    compaction inactivity budget, PR #71508)."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    now = {"ts": 1000.0}

    def progress_fn():
        # api_call_count and current_tool frozen (long streaming response in
        # flight), but the activity timestamp advances with every chunk.
        now["ts"] += 1.0
        return ((1, None, now["ts"]),), False

    res = ad.dispatch_async_delegation(
        goal="streaming child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: (gate.wait(timeout=10), {"status": "completed", "summary": "streamed"})[1],
        progress_fn=progress_fn,
    )
    assert res["status"] == "dispatched"

    time.sleep(0.6)  # several sweeps past the shrunk idle threshold
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"


def test_stalled_event_carries_structured_stall_metadata(monkeypatch):
    """The terminal stalled event must expose machine-readable stall context
    (#51690) — quiet duration, tripped threshold, phase, grace — mirroring
    the sync path's timeout_seconds/timed_out_after_seconds/timeout_phase."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()

    res = ad.dispatch_async_delegation(
        goal="stall metadata", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: {} if gate.wait(timeout=10) else {},
        progress_fn=lambda: ((0, "terminal"), True),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["status"] == "stalled"
        assert evt["stalled_after_quiet_seconds"] >= 0.3  # in-tool threshold
        assert evt["stall_threshold_seconds"] == ad._STALE_IN_TOOL_SECONDS
        assert evt["stall_phase"] == "in_tool"
        assert evt["stall_grace_seconds"] == ad._STALL_GRACE_SECONDS
    finally:
        gate.set()


def test_list_async_delegations_exposes_live_activity(monkeypatch):
    """list_async_delegations must expose per-child live activity sampled
    from progress_fn plus seconds_since_progress, for /agents UIs (#51690)."""
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.03)
    gate = threading.Event()
    base_ts = time.time() - 12.0

    res = ad.dispatch_async_delegation(
        goal="live listing", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: {} if gate.wait(timeout=10) else {},
        progress_fn=lambda: (((3, "web_search", base_ts),), True),
    )
    try:
        time.sleep(0.1)  # let the monitor stamp _progress_ts at least once
        item = next(
            d for d in ad.list_async_delegations()
            if d["delegation_id"] == res["delegation_id"]
        )
        assert item["status"] == "running"
        assert item["in_tool"] is True
        assert "seconds_since_progress" in item
        (child,) = item["children_activity"]
        assert child["api_calls"] == 3
        assert child["current_tool"] == "web_search"
        assert 10.0 <= child["seconds_since_activity"] <= 20.0
        # Callables and private bookkeeping must never leak.
        assert "progress_fn" not in item
        assert "interrupt_fn" not in item
        assert not any(k.startswith("_") for k in item)
    finally:
        gate.set()


def test_in_tool_stall_uses_higher_threshold(monkeypatch):
    """A frozen child inside a tool gets the in-tool ceiling, not the idle one."""
    _fast_stale_monitor(monkeypatch, idle=0.1, in_tool=10.0, grace=0.1)
    gate = threading.Event()

    def runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "long tool finished"}

    res = ad.dispatch_async_delegation(
        goal="long tool child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=runner, max_async_children=1,
        # Frozen token but in_tool=True — a legitimately slow terminal
        # command / web fetch. Must NOT be stalled at the idle threshold.
        progress_fn=lambda: ((1, "terminal"), True),
    )
    assert res["status"] == "dispatched"

    time.sleep(0.5)  # far past idle threshold, well under in-tool threshold
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"


def test_real_process_restart_restores_owned_completion_once(tmp_path):
    """Real-import E2E: a fresh interpreter restores a prior process's result."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo}
    producer = r'''
import time
from tools import async_delegation as ad
r = ad.dispatch_async_delegation(
    goal="restart", context=None, toolsets=None, role="leaf", model="m",
    session_key="owner-session", parent_session_id="durable-parent",
    runner=lambda: {"status": "completed", "summary": "after restart"},
)
deadline = time.time() + 5
while ad.active_count() and time.time() < deadline:
    time.sleep(.01)
print(r["delegation_id"])
'''
    first = subprocess.run(
        [sys.executable, "-c", producer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    delegation_id = first.stdout.strip().splitlines()[-1]

    # The ledger replays on the first consumer, not at import (#123265).
    consumer = r'''
import json
from tools.process_registry import process_registry
process_registry.restore_completions()
evt = process_registry.completion_queue.get_nowait()
print(json.dumps(evt, sort_keys=True))
'''
    second = subprocess.run(
        [sys.executable, "-c", consumer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    evt = json.loads(second.stdout.strip().splitlines()[-1])
    assert evt["delegation_id"] == delegation_id
    assert evt["session_key"] == "owner-session"
    assert evt["parent_session_id"] == "durable-parent"
    assert evt["summary"] == "after restart"

    acker = f'''
from tools import async_delegation as ad
assert ad.mark_completion_delivered({delegation_id!r})
'''
    subprocess.run(
        [sys.executable, "-c", acker], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    probe = subprocess.run(
        [sys.executable, "-c", "from tools.process_registry import process_registry; process_registry.restore_completions(); print(process_registry.completion_queue.qsize())"],
        cwd=repo, env=env, text=True, capture_output=True, timeout=15, check=True,
    )
    assert probe.stdout.strip().splitlines()[-1] == "0"


def test_real_process_restart_reports_queued_work_as_interrupted(tmp_path):
    """Queued work owned by a dead process is surfaced, not silently dropped."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo}
    producer = r'''
import json
import threading
from tools import async_delegation as ad
hold = threading.Event()
first = ad.dispatch_async_delegation(
    goal="occupy", context=None, toolsets=None, role="leaf", model="m",
    session_key="owner-session", parent_session_id="queued-parent",
    runner=lambda: (hold.wait(30), {"status": "completed"})[1],
    max_async_children=1, max_queued_delegations=1,
)
queued = ad.dispatch_async_delegation(
    goal="queued work", context=None, toolsets=None, role="leaf", model="m",
    session_key="owner-session", parent_session_id="queued-parent",
    runner=lambda: {"status": "completed", "summary": "must not run after owner exit"},
    max_async_children=1, max_queued_delegations=1,
)
assert first["status"] == "dispatched"
assert queued["status"] == "queued"
print(json.dumps({"delegation_id": queued["delegation_id"]}))
'''
    first = subprocess.run(
        [sys.executable, "-c", producer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    queued_id = json.loads(first.stdout.strip().splitlines()[-1])["delegation_id"]

    consumer = r'''
import json
from tools.process_registry import process_registry
process_registry.restore_completions()
events = []
while not process_registry.completion_queue.empty():
    events.append(process_registry.completion_queue.get_nowait())
print(json.dumps(events, sort_keys=True))
'''
    second = subprocess.run(
        [sys.executable, "-c", consumer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    events = json.loads(second.stdout.strip().splitlines()[-1])
    queued_event = next(evt for evt in events if evt.get("delegation_id") == queued_id)
    assert queued_event["status"] == "interrupted"
    assert queued_event["exit_reason"] == "interrupted"
    assert "never started" in queued_event["error"]
    assert queued_event["session_key"] == "owner-session"
    assert queued_event["parent_session_id"] == "queued-parent"


# ---------------------------------------------------------------------------
# Integration: delegate_task(background=True) routing
# ---------------------------------------------------------------------------

def test_delegate_task_background_routes_async_and_does_not_block(monkeypatch):
    """delegate_task(background=True) returns a handle without running the
    child synchronously, and the child completes on the background thread.
    A single task is dispatched as a one-item background batch unit."""
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"

    gate = threading.Event()

    def slow_child(task_index, goal, child=None, parent_agent=None, **kw):
        gate.wait(timeout=60)  # a sync impl would hang delegate_task here
        return {
            "task_index": 0, "status": "completed", "summary": f"done: {goal}",
            "api_calls": 1, "duration_seconds": 0.1, "model": "m",
            "exit_reason": "completed",
        }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    # monkeypatch (not `with`) so patches outlive delegate_task's return and
    # remain active while the background worker runs.
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_run_single_child", slow_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    out = dt.delegate_task(
        goal="the real task", context="ctx",
        background=True, parent_agent=parent,
    )

    import json
    parsed = json.loads(out)
    assert parsed["status"] == "dispatched"
    assert parsed["mode"] == "background"
    assert parsed["delegation_id"].startswith("deleg_")
    # Non-blocking invariant: delegate_task returned while the child is STILL
    # blocked on the closed gate, so no completion event exists yet.
    assert process_registry.completion_queue.empty()
    assert ad.active_count() == 1  # one background batch unit, not finished

    gate.set()
    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    # Single task rides the batch path → carries a 1-item results list.
    assert evt.get("is_batch") is True
    assert len(evt["results"]) == 1
    assert evt["results"][0]["summary"] == "done: the real task"
    text = format_process_notification(evt)
    assert text is not None
    assert "the real task" in text


def test_delegate_task_background_uses_live_tui_agent_session_id(monkeypatch):
    """TUI async delegation must route to the live/compressed agent id.

    Regression: delegate_task captured the stale approval/session context key
    after compression rotated parent_agent.session_id. The resulting completion
    was orphaned and could be consumed by an unrelated desktop session poller.
    """
    import json
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.approval_context import reset_current_session_key, set_current_session_key

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "post-compress-tip"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    monkeypatch.setattr(
        dt,
        "_run_single_child",
        lambda *a, **k: {
            "task_index": 0,
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "model": "m",
            "exit_reason": "completed",
        },
    )

    approval_token = set_current_session_key("pre-compress-parent")
    session_tokens = set_session_vars(
        source="tui",
        session_key="pre-compress-parent",
        ui_session_id="origin-tab",
    )
    try:
        out = dt.delegate_task(goal="bg task", background=True, parent_agent=parent)
        assert json.loads(out)["status"] == "dispatched"
        evt = _drain_one()
    finally:
        reset_current_session_key(approval_token)
        clear_session_vars(session_tokens)

    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["session_key"] == "post-compress-tip"
    assert evt["origin_ui_session_id"] == "origin-tab"


def test_concurrent_dispatch_respects_capacity():
    """Two threads racing dispatch with cap=1 must yield exactly one accept
    (capacity check and record insert are atomic under the records lock)."""
    gate = threading.Event()

    def blocker():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "x"}

    results = []
    barrier = threading.Barrier(2)

    def racer():
        barrier.wait(timeout=5)
        results.append(
            ad.dispatch_async_delegation(
                goal="race", context=None, toolsets=None, role="leaf",
                model="m", session_key="", runner=blocker,
                max_async_children=1, max_queued_delegations=0,
            )
        )

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["dispatched", "rejected"]
    gate.set()


# ---------------------------------------------------------------------------
# Gateway routing: session_key -> platform/chat_id, rich formatting, injection
# ---------------------------------------------------------------------------

def _make_async_evt(**over):
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_x1",
        "session_key": "agent:main:telegram:dm:12345:678",
        "goal": "Investigate flaky test",
        "context": "repo /tmp/p",
        "toolsets": ["terminal"],
        "role": "leaf",
        "model": "m",
        "status": "completed",
        "summary": "Found the bug in test_foo",
        "api_calls": 4,
        "duration_seconds": 12.0,
        "dispatched_at": 1000.0,
        "completed_at": 1012.0,
    }
    evt.update(over)
    return evt


def test_gateway_formatter_renders_async_block():
    from gateway.run import _format_gateway_process_notification

    txt = _format_gateway_process_notification(_make_async_evt())
    assert txt is not None
    assert "ASYNC DELEGATION COMPLETE" in txt
    assert "Found the bug in test_foo" in txt
    assert "Investigate flaky test" in txt


def test_gateway_cli_origin_event_left_unrouted():
    """An empty session_key (CLI origin) is left without routing fields."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    evt = _make_async_evt(session_key="")
    runner._enrich_async_delegation_routing(evt)
    assert "platform" not in evt


def test_single_task_truncation_banner_when_max_iterations():
    """A single async subagent that hit its iteration cap (exit_reason=
    max_iterations) must surface a TRUNCATED marker in the formatted result,
    even though status stays 'completed' (a summary exists)."""
    evt = _make_async_evt(
        status="completed",
        summary="Did part of the work then ran out of budget.",
        exit_reason="max_iterations",
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "TRUNCATED" in text
    assert "max_iterations" in text
    # The summary is still shown, just flagged.
    assert "Did part of the work" in text


def test_single_task_no_banner_when_clean():
    """A cleanly-finished subagent must NOT get a truncation banner."""
    evt = _make_async_evt(status="completed", summary="All done.", exit_reason="completed")
    text = format_process_notification(evt)
    assert text is not None
    assert "TRUNCATED" not in text


def test_batch_truncation_banner_marks_only_truncated_task():
    """In a batch, only the task that hit max_iterations gets the TRUNCATED
    marker; a clean sibling keeps the normal check icon."""
    evt = _make_async_evt(
        is_batch=True,
        goals=["clean task", "truncated task"],
        results=[
            {
                "task_index": 0,
                "status": "completed",
                "summary": "finished cleanly",
                "api_calls": 5,
                "exit_reason": "completed",
                "truncated": False,
            },
            {
                "task_index": 1,
                "status": "completed",
                "summary": "cut off mid-work",
                "api_calls": 250,
                "exit_reason": "max_iterations",
                "truncated": True,
            },
        ],
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "TRUNCATED" in text
    # The clean task's summary and the truncated one's both render...
    assert "finished cleanly" in text
    assert "cut off mid-work" in text
    # ...but the banner is tied to the truncated task, not the clean one.
    trunc_pos = text.index("cut off mid-work")
    clean_pos = text.index("finished cleanly")
    banner_pos = text.index("TRUNCATED")
    # The header banner for task 2 appears after task 1's summary.
    assert banner_pos > clean_pos


def _patch_delegation_cfg(monkeypatch, model="upstage/solar-pro-4", provider="openrouter"):
    """Pin the delegation config the notice renderer reads (adapts the
    #97667 tests to the shipped implementation, which reads the configured
    model from config rather than the event's model field)."""
    import tools.process_registry_notifications as _prn

    monkeypatch.setattr(
        _prn, "_delegation_config", lambda: {"model": model, "provider": provider}
    )


def test_batch_model_rejection_notice_prepended(monkeypatch):
    """A rejected delegation model must surface ONE config-level notice above
    the per-task blocks instead of staying buried in each summary (#97654)."""
    rejection = "HTTP 400: upstage/solar-pro-4 is not a valid model ID"
    _patch_delegation_cfg(monkeypatch)
    evt = _make_async_evt(
        is_batch=True,
        model="upstage/solar-pro-4",
        goals=["task a", "task b"],
        results=[
            {
                "task_index": 0,
                "status": "completed",
                "summary": rejection,
                "api_calls": 1,
                "duration_seconds": 0.74,
                "exit_reason": "max_iterations",
                "truncated": True,
            },
            {
                "task_index": 1,
                "status": "completed",
                "summary": rejection,
                "api_calls": 1,
                "duration_seconds": 0.71,
                "exit_reason": "max_iterations",
                "truncated": True,
            },
        ],
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "SUBAGENT MODEL REJECTED" in text
    assert "upstage/solar-pro-4" in text
    assert "delegation.model" in text
    # The notice precedes the per-task blocks, not just trails them.
    assert text.index("SUBAGENT MODEL REJECTED") < text.index("TASK 1/2")


def test_batch_model_rejection_notice_absent_when_clean(monkeypatch):
    """Ordinary summaries must not grow a model-rejection notice."""
    _patch_delegation_cfg(monkeypatch, model="upstage/solar-pro4")
    evt = _make_async_evt(
        is_batch=True,
        model="upstage/solar-pro4",
        goals=["task a"],
        results=[
            {
                "task_index": 0,
                "status": "completed",
                "summary": "did the work",
                "api_calls": 3,
                "exit_reason": "completed",
                "truncated": False,
            },
        ],
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "SUBAGENT MODEL REJECTED" not in text


def test_batch_model_rejection_notice_requires_configured_model_in_text(monkeypatch):
    """A model_not_found pattern naming a DIFFERENT model than the configured
    delegation model is task-level noise, not a config-level rejection."""
    _patch_delegation_cfg(monkeypatch, model="upstage/solar-pro4")
    evt = _make_async_evt(
        is_batch=True,
        model="upstage/solar-pro4",
        goals=["task a"],
        results=[
            {
                "task_index": 0,
                "status": "completed",
                "summary": "HTTP 400: other/model-x is not a valid model ID",
                "api_calls": 1,
                "exit_reason": "max_iterations",
                "truncated": True,
            },
        ],
    )
    text = format_process_notification(evt)
    assert text is not None
    assert "SUBAGENT MODEL REJECTED" not in text


# ---------------------------------------------------------------------------
# Per-group completion units: ungrouped tasks return alone, a `group` returns
# together, and the units of one call share ONE capacity slot.
# ---------------------------------------------------------------------------

def _grouped_fanout(monkeypatch, tasks, gates):
    """delegate_task(tasks) in the background with gated fake children; returns the parsed handle."""
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None

    def child(task_index, goal, child=None, parent_agent=None, **kw):
        gates[task_index].wait(timeout=60)
        return {"task_index": task_index, "status": "completed", "summary": f"done: {goal}", "api_calls": 1,
                "duration_seconds": 0.1, "model": "m", "exit_reason": "completed"}

    def build(**kw):
        c = MagicMock()
        c._delegate_role = "leaf"
        c._subagent_id = f"s{kw['task_index']}"
        return c

    creds = {"model": "m", "provider": None, "base_url": None, "api_key": None, "api_mode": None, "command": None,
             "args": None}
    monkeypatch.setattr(dt, "_build_child_agent", build)
    monkeypatch.setattr(dt, "_run_single_child", child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    return json.loads(dt.delegate_task(tasks=tasks, background=True, parent_agent=parent))


def test_ungrouped_task_completes_alone_and_group_completes_together(monkeypatch):
    """With delegation.independent_completions on, a finished ungrouped task must not wait for its siblings;
    tasks sharing a `group` must."""
    import tools.delegate_tool as dt
    monkeypatch.setattr(dt, "_load_config", lambda: {"independent_completions": True})
    gates = [threading.Event() for _ in range(4)]
    tasks = [
        {"goal": "review PR 1 thoroughly and report"},
        {"goal": "compare approach A in detail", "group": "cmp"},
        {"goal": "compare approach B in detail", "group": "cmp"},
        {"goal": "review PR 2 thoroughly and report"},
    ]
    handle = _grouped_fanout(monkeypatch, tasks, gates)
    assert handle["status"] == "dispatched"
    by_group = {tuple(u["task_indexes"]): u["group"] for u in handle["units"]}
    assert by_group == {(0,): None, (1, 2): "cmp", (3,): None}

    gates[3].set()
    evt = _drain_one()
    assert [r["task_index"] for r in evt["results"]] == [3]  # PR 2 landed while everything else still runs
    assert "review PR 2" in format_process_notification(evt)

    gates[1].set()
    assert _drain_one(timeout=0.5) is None  # half a group is not a completion
    gates[2].set()
    evt = _drain_one()
    assert evt["group"] == "cmp" and [r["task_index"] for r in evt["results"]] == [1, 2]

    gates[0].set()
    assert [r["task_index"] for r in _drain_one()["results"]] == [0]


def test_multi_unit_background_payload_uses_per_unit_handles():
    """A split call must not expose unit 1 as a misleading call-wide cancellation handle."""
    from tools.delegate_tool_dispatch import _Batch, _dispatched_payload

    tasks = [{"goal": "one"}, {"goal": "two"}]
    batch = _Batch(
        task_list=tasks, children=[], parent_agent=object(), creds={"model": "m"}, context=None,
        top_role="leaf", max_children=2, live_deleg_id="call-id", live_writers=[], live_paths=[],
        origin_wake_sid="", origin_ui_session_id="", origin_owner_transport=None,
        origin_owner_session_record=None, origin_session_history_delivery=False, overall_start=time.monotonic(),
    )
    units = [replace(batch, children=[(0, tasks[0], object())]), replace(batch, children=[(1, tasks[1], object())])]
    payload = _dispatched_payload(batch, [(units[0], "call-id-1"), (units[1], "call-id-2")])
    assert "delegation_id" not in payload
    assert [unit["delegation_id"] for unit in payload["units"]] == ["call-id-1", "call-id-2"]


def test_partial_multi_unit_payload_uses_accepted_unit_handle():
    """A partial multi-unit response must identify the runnable unit, not the bare call."""
    from tools.delegate_tool_dispatch import _Batch, _dispatched_payload

    tasks = [{"goal": "one"}, {"goal": "two"}]
    batch = _Batch(
        task_list=tasks, children=[], parent_agent=object(), creds={"model": "m"}, context=None,
        top_role="leaf", max_children=2, live_deleg_id="call-id", live_writers=[], live_paths=[],
        origin_wake_sid="", origin_ui_session_id="", origin_owner_transport=None,
        origin_owner_session_record=None, origin_session_history_delivery=False, overall_start=time.monotonic(),
    )
    unit = replace(batch, children=[(1, tasks[1], object())])
    payload = _dispatched_payload(batch, [(unit, "call-id-2")])
    assert payload["delegation_id"] == "call-id-2"


def test_units_of_one_call_share_a_single_capacity_slot():
    """Splitting a call into per-task completions must not consume more pool capacity than the call did."""
    gate = threading.Event()

    def blocker():
        gate.wait(timeout=60)
        return {"results": [], "total_duration_seconds": 0}

    common = dict(goals=["a", "b"], context=None, toolsets=None, role="leaf", model="m", session_key="",
                  runner=blocker, max_async_children=1)
    first = ad.dispatch_async_delegation_batch(delegation_id="deleg_call-1", task_indexes=[0], **common)
    second = ad.dispatch_async_delegation_batch(delegation_id="deleg_call-2", task_indexes=[1],
                                                slot_key="deleg_call-1", **common)
    other = ad.dispatch_async_delegation_batch(delegation_id="deleg_other", max_queued_delegations=1, **common)
    assert (first["status"], second["status"], other["status"]) == ("dispatched", "dispatched", "queued")
    assert ad.active_task_count() == 2
    gate.set()
    # The two slot-sharing units finish in either order once the gate opens, so collect every
    # expected completion instead of draining for one id at a time (which discards the other).
    wanted = {"deleg_call-1", "deleg_call-2", "deleg_other"}
    seen = set()
    deadline = time.monotonic() + 10.0
    while seen != wanted and time.monotonic() < deadline:
        try:
            evt = process_registry.completion_queue.get(timeout=0.05)
        except queue.Empty:
            continue
        if evt.get("delegation_id") in wanted:
            seen.add(evt["delegation_id"])
    assert seen == wanted


def test_multi_task_call_is_one_completion_unless_independent_completions(monkeypatch):
    """Default: a background fan-out returns as ONE message when every task is done, so an orchestrator
    is not woken N times per call; `group` is inert until delegation.independent_completions is on."""
    import tools.delegate_tool as dt
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    gates = [threading.Event() for _ in range(3)]
    tasks = [{"goal": "review PR 1 thoroughly and report"}, {"goal": "review PR 2 thoroughly and report", "group": "g"},
             {"goal": "review PR 3 thoroughly and report", "group": "g"}]
    handle = _grouped_fanout(monkeypatch, tasks, gates)
    assert handle["status"] == "dispatched" and "units" not in handle
    gates[0].set()
    gates[1].set()
    assert _drain_one(timeout=0.5) is None  # two of three done: no message yet
    gates[2].set()
    evt = _drain_one()
    assert sorted(r["task_index"] for r in evt["results"]) == [0, 1, 2]


def test_units_beyond_slot_count_still_start_and_are_not_stalled_while_queued(monkeypatch):
    """Units of one call share a slot, so live units can exceed the slot cap; every unit must still get a worker,
    and a unit must not be judged stalled for time it spent waiting to start."""
    _fast_stale_monitor(monkeypatch, idle=0.3, grace=0.2)
    started, release = [], threading.Event()
    frozen = lambda: (((0, None, None),), False)  # noqa: E731 - child never progresses => token never changes

    def blocker(uid):
        def run():
            started.append(uid)
            release.wait(timeout=10)
            return {"results": [{"task_index": 0, "status": "completed"}], "total_duration_seconds": 0}
        return run

    common = dict(goals=["x"], context=None, toolsets=None, role="leaf", model="m", session_key="", max_async_children=1)
    ad.dispatch_async_delegation_batch(delegation_id="deleg_c-1", runner=blocker("c-1"), progress_fn=frozen, **common)
    ad.dispatch_async_delegation_batch(delegation_id="deleg_c-2", runner=blocker("c-2"), slot_key="deleg_c-1", **common)
    deadline = time.monotonic() + 2.0
    while len(started) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sorted(started) == ["c-1", "c-2"]  # second unit started despite a 1-slot pool

    # A unit whose runner has NOT started yet must not accrue stall time: pin the pool so it stays queued.
    monkeypatch.setattr(ad, "_get_executor", lambda n: ad._executor)
    ad.dispatch_async_delegation_batch(delegation_id="deleg_q", runner=blocker("q"), progress_fn=frozen,
                                       **{**common, "max_async_children": 3})
    time.sleep(0.7)  # > idle + grace with the unit still queued
    with ad._records_lock:
        assert ad._records["deleg_q"]["status"] == "running"
    assert "q" not in started
    release.set()


def test_child_finished_before_crash_is_recovered_with_its_result(tmp_path):
    """Real-import E2E: a 2-task group unit whose owner dies mid-run replays the finished child's real result
    and marks only the unfinished sibling unknown — a crash costs the stragglers, never the finished work."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo}
    producer = r'''
import os, sys, time
from unittest.mock import MagicMock
import tools.delegate_tool as dt
parent = MagicMock(); parent._delegate_depth = 0; parent.session_id = "sess"; parent._interrupt_requested = False
parent._active_children = []; parent._active_children_lock = None
def child(task_index, goal, child=None, parent_agent=None, **kw):
    if task_index == 1:
        time.sleep(600)
    return {"task_index": task_index, "status": "completed", "summary": f"done: {goal}", "api_calls": 1,
            "duration_seconds": 0.1, "model": "m", "exit_reason": "completed"}
def build(**kw):
    c = MagicMock(); c._delegate_role = "leaf"; c._subagent_id = f"s{kw['task_index']}"; return c
creds = {"model": "m", "provider": None, "base_url": None, "api_key": None, "api_mode": None, "command": None, "args": None}
dt._build_child_agent = build; dt._run_single_child = child; dt._resolve_delegation_credentials = lambda *a, **k: creds
dt.delegate_task(tasks=[{"goal": "fast member of the group task", "group": "g"},
                        {"goal": "slow member of the group task", "group": "g"}], background=True, parent_agent=parent)
time.sleep(2.0)
sys.stdout.flush(); os._exit(1)
'''
    subprocess.run([sys.executable, "-c", producer], cwd=repo, env=env, text=True, capture_output=True, timeout=30)
    consumer = r'''
import json, queue
from tools import async_delegation as ad
q = queue.Queue(); ad.restore_undelivered_completions(q)
print(json.dumps(q.get_nowait(), sort_keys=True))
'''
    second = subprocess.run([sys.executable, "-c", consumer], cwd=repo, env=env, text=True, capture_output=True,
                            timeout=15, check=True)
    evt = json.loads(second.stdout.strip().splitlines()[-1])
    by_index = {r["task_index"]: r for r in evt["results"]}
    assert by_index[0]["status"] == "completed" and by_index[0]["summary"] == "done: fast member of the group task"
    assert by_index[1]["status"] == "unknown"
    assert "1/2 child results were recorded" in evt["error"]
    assert "done: fast member" in format_process_notification(evt)


def test_one_child_unit_keeps_its_finished_child_when_the_owner_dies(tmp_path):
    """#116000: a detached unit with exactly ONE child had NO durable record of that child at all — only the
    multi-child join path called ``record_unit_child`` — so an owner death (OOM-kill / orphaning) anywhere in the
    window after the child returned (host-owned finalize, transcripts, manifest, then the durable completion write)
    replayed a bare "outcome unknown" and threw the finished work away. Real-import E2E: a one-task background
    ``delegate_task`` child completes, the owner is killed while blocked inside that window, and a fresh process
    must replay the child's real result to the parent."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    marker = tmp_path / "child-returned.flag"
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo, "REPRO_MARKER": str(marker)}
    producer = r'''
import os, sys, time
from unittest.mock import MagicMock
import tools.delegate_tool as dt
import tools.delegate_tool_dispatch as dtd
parent = MagicMock(); parent._delegate_depth = 0; parent.session_id = "sess"; parent._interrupt_requested = False
parent._active_children = []; parent._active_children_lock = None
def child(task_index, goal, child=None, parent_agent=None, **kw):
    return {"task_index": task_index, "status": "completed", "summary": f"done: {goal}", "api_calls": 1,
            "duration_seconds": 0.1, "model": "m", "exit_reason": "completed"}
def build(**kw):
    c = MagicMock(); c._delegate_role = "leaf"; c._subagent_id = f"s{kw['task_index']}"; return c
creds = {"model": "m", "provider": None, "base_url": None, "api_key": None, "api_mode": None, "command": None, "args": None}
dt._build_child_agent = build; dt._run_single_child = child; dt._resolve_delegation_credentials = lambda *a, **k: creds
def held_finalize(*a, **k):
    # The child's result exists; the owner still has host-owned finalize + transcripts + manifest + the durable
    # write to do. Block HERE so the driver kills the owner inside that window: deterministic, no race.
    open(os.environ["REPRO_MARKER"], "w").write("child-returned")
    time.sleep(600)
dtd._finalize_child_results = held_finalize
dt.delegate_task(tasks=[{"goal": "single background subagent"}], background=True, parent_agent=parent)
time.sleep(600)
'''
    proc = subprocess.Popen([sys.executable, "-u", "-c", producer], cwd=repo, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the child never returned, so the death window was never reached"
    finally:
        proc.kill()
        proc.wait(timeout=20)
        time.sleep(0.3)  # let the OS reap the owner before recovery asks whether its pid is alive
    consumer = r'''
import json, queue
from tools import async_delegation as ad
q = queue.Queue(); ad.restore_undelivered_completions(q)
print(json.dumps(q.get_nowait(), sort_keys=True))
'''
    second = subprocess.run([sys.executable, "-u", "-c", consumer], cwd=repo, env=env, text=True,
                            capture_output=True, timeout=30, check=True)
    evt = json.loads(second.stdout.strip().splitlines()[-1])
    (entry,) = evt["results"]  # the finished child, not a fabricated "unknown"
    assert entry["status"] == "completed" and entry["summary"] == "done: single background subagent"
    assert "1/1 child results were recorded" in evt["error"]
    assert "done: single background subagent" in format_process_notification(evt)


@pytest.mark.platforms("posix")  # POSIX mode bits not enforced on Windows
def test_connect_creates_state_db_0o600_under_permissive_umask(tmp_path, monkeypatch):
    """``_connect`` shares state.db with hermes_state.SessionDB -- a fresh
    HERMES_HOME must land the file (and its WAL sidecar, if created) at 0o600
    even under a permissive process umask, not the SessionDB-only path."""
    import stat

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    old_umask = os.umask(0o022)
    try:
        conn = ad._connect()
        conn.close()
    finally:
        os.umask(old_umask)

    db_path = tmp_path / "state.db"
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    for suffix in ("-wal", "-shm"):
        sidecar = tmp_path / f"state.db{suffix}"
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_persist_failure_still_delivers_result_and_frees_slot(monkeypatch):
    """A failing terminal durable write (locked/full state.db) must not eat the completion
    event or park the record on ``finalizing`` (#76605, #112030): the event is the only delivery
    path and ``finalizing`` counts against ``max_concurrent_children`` forever."""
    def boom(event, result):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ad, "_persist_completion", boom)
    res = ad.dispatch_async_delegation(
        goal="g", context=None, toolsets=None, role="leaf", model="m", session_key="",
        runner=lambda: {"status": "completed", "summary": "done"}, max_async_children=1,
    )
    evt = _drain_for(res["delegation_id"])

    assert evt is not None and evt["status"] == "completed" and evt["summary"] == "done"
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ad.active_count() == 0
    with ad._records_lock:
        assert ad._records[res["delegation_id"]]["status"] == "completed"


def test_prune_never_evicts_live_records():
    """Retention pruning drops TERMINAL records only; ``stalling``/``finalizing`` are live work
    (#76605, #112030). A stalling record has no ``completed_at`` so it sorts oldest and was the
    first eviction candidate, sending its late runner return into the missing-record path."""
    with ad._records_lock:
        for status, ts in (("stalling", 1.0), ("finalizing", 2.0), ("running", 3.0)):
            ad._records[f"live-{status}"] = {"delegation_id": f"live-{status}", "status": status,
                                             "dispatched_at": ts, "completed_at": None}
        for i in range(ad._MAX_RETAINED_COMPLETED + 1):
            ad._records[f"done-{i}"] = {"delegation_id": f"done-{i}", "status": "completed",
                                        "dispatched_at": 100.0 + i, "completed_at": 200.0 + i}
        ad._prune_completed_locked()
        survivors = set(ad._records)

    assert {"live-stalling", "live-finalizing", "live-running"} <= survivors
    assert "done-0" not in survivors and len(survivors - {"live-stalling", "live-finalizing", "live-running"}) == ad._MAX_RETAINED_COMPLETED



@pytest.mark.parametrize("batch", [False, True])
def test_completion_notice_does_not_echo_oversized_task_source(batch):
    """Regression: a huge dispatch context (a full review diff) was echoed verbatim into the completion
    message; that 947 KB user turn then sat in the protected tail and blocked compression. The parent
    already holds the context in its own transcript, so the notice keeps only a bounded reminder."""
    head_marker, tail_marker = "REVIEW-INSTRUCTIONS-HEAD", "DIFF-TAIL-MARKER"
    context = head_marker + "\n" + ("-removed line of a large diff\n" * 40_000) + tail_marker
    result_text = "Verdict: approve. Only low-severity leftovers."
    common = dict(context=context, toolsets=None, role="leaf", model="m", session_key="", max_async_children=3)
    if batch:
        handle = ad.dispatch_async_delegation_batch(
            goals=["Review candidate abc"],
            runner=lambda: {"results": [{"task_index": 0, "status": "completed", "summary": result_text}]},
            **common)
    else:
        handle = ad.dispatch_async_delegation(
            goal="Review candidate abc",
            runner=lambda: {"status": "completed", "summary": result_text, "api_calls": 3},
            **common)
    evt = _drain_for(handle["delegation_id"])
    assert evt is not None
    text = format_process_notification(evt)
    assert result_text in text
    assert head_marker in text, "the start of the dispatched context still orients the parent"
    assert tail_marker not in text, "the bulk of an oversized context must not be echoed back"
    assert len(text) < len(context) // 10
