"""Finite-work retirement and file-backed process transfer, without native services."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import delivery_ledger as ledger, zero_cut_handover_drain as drain
from tools import async_delegation, process_registry as process_module
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.mark.parametrize("state,attempts,error,age,expected", [
    ("pending", 0, None, 0, 1),
    ("attempting", 0, None, 0, 1),
    ("failed", 0, "temporary send failure", 0, 1),
    ("failed", 2, "temporary send failure", 0, 0),
    ("failed", 0, "Forbidden: bot was blocked by the user", 0, 0),
    ("failed", 0, "temporary send failure", ledger.STALE_AFTER_SECONDS + 1, 0),
    ("failed", ledger.MAX_ATTEMPTS, "send_path_degraded: reconnect", 0, 0),
    ("delivered", 0, None, 0, 0),
])
def test_retirement_counts_only_runtime_settleable_deliveries(monkeypatch, tmp_path, state, attempts, error, age, expected):
    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")
    ledger.record_obligation(obligation_id="reply", session_key="telegram:test", platform="telegram",
                             chat_id="test", thread_id=None, content="original reply")
    now = time.time()
    with ledger._transaction() as conn:
        conn.execute("""UPDATE delivery_obligations SET state=?, attempts=?, last_error=?,
                        created_at=?, updated_at=? WHERE obligation_id='reply'""",
                     (state, attempts, error, now - age, now))
    assert ledger.owned_pending_count() == expected
    # Retirement eligibility never spends a send attempt or rewrites the parked result.
    with ledger._transaction() as conn:
        assert conn.execute("SELECT state, attempts, content FROM delivery_obligations").fetchone() == (
            state, attempts, "original reply")


@pytest.mark.parametrize("state", ["pending", "attempting"])
def test_orphaned_inflight_delivery_does_not_hold_retirement(monkeypatch, tmp_path, state):
    now = time.time()
    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(ledger.time, "time", lambda: now)
    monkeypatch.setattr(process_module, "process_registry", ProcessRegistry())
    monkeypatch.setattr(async_delegation, "owned_pending_count", lambda: 0)
    monkeypatch.setattr("tools.approval.pending_gateway_approval_count", lambda: 0)
    ledger.record_obligation(obligation_id="reply", session_key="telegram:test", platform="telegram",
                             chat_id="test", thread_id=None, content="original reply")
    if state == "attempting":
        ledger.mark_attempting("reply")
    # A retry can start long after creation. Last progress, not creation age,
    # bounds the in-flight hold, and retirement must not mutate ambiguous sends.
    with ledger._transaction() as conn:
        conn.execute("UPDATE delivery_obligations SET created_at=?", (now - ledger.STALE_AFTER_SECONDS - 1,))
        original = conn.execute("SELECT * FROM delivery_obligations").fetchall()
    runner = SimpleNamespace(_running_agent_count=lambda: 0, adapters={},
                             _zero_cut_handover_state={"token_lock_released": True})
    assert drain._owned_work(runner) == 1
    assert drain._status(runner)["idle"] is False
    now += ledger.ATTEMPT_ORDER_HOLD_SECONDS
    assert drain._owned_work(runner) == 0, "An orphaned in-flight row must not retain its live owner forever"
    assert drain._status(runner)["idle"] is True
    # A genuinely active final send still owns the predecessor after this age.
    runner.adapters = {"telegram": SimpleNamespace(platform="telegram", _active_sessions={"telegram:test": object()})}
    assert drain._status(runner)["idle"] is False
    runner.adapters.clear()
    with ledger._transaction() as conn:
        assert conn.execute("SELECT * FROM delivery_obligations").fetchall() == original


@pytest.mark.parametrize("persistent,survivor,file_backed,expected", [
    (False, False, False, 1),
    (True, False, True, 0),
    (False, True, True, 0),
    (True, False, False, 1),
])
def test_retirement_waits_only_for_processes_that_cannot_transfer(monkeypatch, persistent, survivor, file_backed, expected):
    registry = ProcessRegistry()
    monkeypatch.setattr(process_module, "process_registry", registry)
    monkeypatch.setattr(ledger, "owned_pending_count", lambda: 0)
    monkeypatch.setattr(async_delegation, "owned_pending_count", lambda: 0)
    session = ProcessSession(id="proc_owned", command="owned work", persist_on_release=persistent,
                             survive_restart=survivor, exit_path="durable.exit" if file_backed else "")
    registry._running[session.id] = session
    runner = SimpleNamespace(_running_agent_count=lambda: 0)
    try:
        assert drain._owned_work(runner) == expected
        assert registry.count_running() == 1  # status still includes the transferred job
    finally:
        registry._running.clear()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("survivor", [False, True])
@pytest.mark.parametrize("successor_writes_first", [False, True])
def test_real_file_backed_process_survives_predecessor_exit_and_is_adopted(
        monkeypatch, tmp_path, survivor, successor_writes_first):
    """The registry owner actually exits. The command is adopted, never rerun."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    release = tmp_path / "release"
    command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
        "from pathlib import Path; import time; "
        f"p=Path({str(release)!r}); "
        "exec('while not p.exists(): time.sleep(0.02)'); print('original completion', flush=True)"
    )
    producer = subprocess.run([sys.executable, "-c", textwrap.dedent("""
        import json, os, sys
        from tools.process_registry import ProcessRegistry
        registry = ProcessRegistry()
        session = registry.spawn_local(sys.argv[1], cwd=sys.argv[2],
            persist_on_release=True, survive_restart=sys.argv[3] == "True")
        session.notify_on_complete = True
        registry._write_checkpoint()
        assert registry.kill_all(source="gateway_handover") == 0
        print(json.dumps({"id": session.id, "pid": session.pid}), flush=True)
        registry.stop_survivor_pollers()
    """), command, str(tmp_path), str(survivor)], capture_output=True, text=True, timeout=20,
        cwd=Path(__file__).resolve().parents[2], env=os.environ.copy())
    registry = ProcessRegistry()
    try:
        assert producer.returncode == 0, producer.stderr
        original = json.loads(producer.stdout.splitlines()[-1])
        if successor_writes_first:
            registry._write_checkpoint()  # ordinary successor activity before its adoption tick
        assert registry.recover_from_checkpoint() == 1
        adopted = registry.get(original["id"])
        assert adopted is not None and adopted.pid == original["pid"]
        assert not adopted.exited
        release.touch()
        # wait() consumes the result and intentionally suppresses its notification.
        # Join the actual supervisor instead, then exercise the notification route.
        reader = adopted._reader_thread
        assert reader is not None
        reader.join(timeout=10)
        assert not reader.is_alive()
        result = registry.poll(adopted.id)
        assert result["exit_code"] == 0
        assert result["output_preview"].count("original completion") == 1
        notifications = registry.drain_notifications(skip_poll_observed=False)
        assert len(notifications) == 1
        assert notifications[0][0]["session_id"] == adopted.id
        assert notifications[0][0]["output"].count("original completion") == 1
        registry.acknowledge_survivor_completion(adopted.id)
        assert registry.drain_notifications() == []
        assert registry.recover_from_checkpoint() == 0
        successor = ProcessRegistry()
        try:
            assert successor.recover_from_checkpoint() == 0
        finally:
            successor.stop_survivor_pollers()
    finally:
        release.touch()
        registry.kill_all(source="test_cleanup")
        registry.stop_survivor_pollers()
