"""Restart-loop breaker behavior for planned and unplanned predecessor exits."""

import asyncio
import os
import signal
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import restart_loop_guard as guard
from gateway.run_startup import GatewayStartupMixin


def _resume_runner():
    runner = object.__new__(GatewayStartupMixin)
    entry = SimpleNamespace(
        resume_pending=True, suspended=False, origin=object(), resume_reason="gateway_restart",
    )
    runner.session_store = SimpleNamespace(
        _lock=threading.RLock(), _entries={"session": entry}, _ensure_loaded_locked=lambda: None,
    )
    runner._AUTO_RESUME_REASONS = {"gateway_restart"}
    runner._restart_loop_guard_config = lambda: (3, 60, 300)
    return runner


@pytest.mark.asyncio
async def test_planned_signal_exit_exempts_next_boot_once(tmp_path, monkeypatch):
    from gateway import run, shutdown_forensics, status

    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(guard, "_state_path", lambda: tmp_path / "gateway" / "restart_loop.json")
    monkeypatch.setattr(shutdown_forensics, "snapshot_shutdown_context", lambda *_: None)
    assert status.write_planned_restart_marker(os.getpid())
    predecessor = SimpleNamespace(stop=AsyncMock())
    signal_initiated = [False]
    created = []
    real_create_task = asyncio.create_task

    def record_task(coro):
        task = real_create_task(coro)
        created.append(task)
        return task

    monkeypatch.setattr(run.asyncio, "create_task", record_task)
    run._start_gateway_make_shutdown_signal_handler(predecessor, signal_initiated)(signal.SIGTERM)
    await asyncio.gather(*created)
    predecessor.stop.assert_awaited_once_with(restart=True, service_restart=True)
    assert signal_initiated == [False]
    assert not status._get_planned_restart_marker_path().exists()

    now = time.time()
    monkeypatch.setattr(guard.time, "time", lambda: now)
    assert _resume_runner()._resume_pending_candidates()
    assert guard._load_state() == ([], [now])
    # No new planned shutdown: the durable intent must not exempt another boot.
    monkeypatch.setattr(guard.time, "time", lambda: now + 10)
    assert _resume_runner()._resume_pending_candidates()
    assert guard._load_state() == ([now + 10], [now])


def test_stale_restart_notification_does_not_exempt_boot(tmp_path, monkeypatch):
    from gateway import run

    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setattr(guard, "_state_path", lambda: tmp_path / "gateway" / "restart_loop.json")
    now = time.time()
    monkeypatch.setattr(guard.time, "time", lambda: now)
    marker = tmp_path / ".restart_notify.json"
    marker.write_text('{"platform": "telegram", "chat_id": "123"}', encoding="utf-8")
    os.utime(marker, (now - 301, now - 301))

    assert _resume_runner()._resume_pending_candidates()
    assert guard._load_state() == ([now], [])


def test_planned_exit_is_consumed_on_a_boot_without_pending_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.setattr(guard, "_state_path", lambda: tmp_path / "gateway" / "restart_loop.json")
    guard.record_planned_exit()
    runner = _resume_runner()
    runner.session_store._entries.clear()
    # Adapter reconnects never take shutdown intent or spend boot budget.
    assert runner._resume_pending_candidates(record_boot=False) == []
    assert guard._planned_exit_path().exists()
    assert runner._resume_pending_candidates() == []
    assert not guard._planned_exit_path().exists()
    assert not guard._state_path().exists()
    assert _resume_runner()._resume_pending_candidates()
    assert not guard._load_state()[1]


@pytest.mark.parametrize("contents", ['{"requested_at": 699}', '{"requested_at": true}', '[]', '{broken'])
def test_stale_or_malformed_planned_exit_is_consumed_without_exemption(tmp_path, monkeypatch, contents):
    monkeypatch.setattr(guard, "_state_path", lambda: tmp_path / "restart_loop.json")
    monkeypatch.setattr(guard.time, "time", lambda: 1000)
    guard._planned_exit_path().write_text(contents, encoding="utf-8")
    assert guard.consume_planned_exit() is False
    assert not guard._planned_exit_path().exists()


def test_unwritable_planned_exit_does_not_block_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_state_path", lambda: tmp_path / "restart_loop.json")

    def deny_write(*args, **kwargs):
        raise PermissionError("read-only gateway directory")

    monkeypatch.setattr(guard, "atomic_json_write", deny_write)
    guard.record_planned_exit()
    assert guard.consume_planned_exit() is False
    assert guard.check_and_record(now=1000) is False


@pytest.mark.parametrize("age, expected", [(300, True), (301, False), (600, True)])
def test_restart_marker_mtime_uses_configured_chain_gap(tmp_path, monkeypatch, age, expected):
    marker = tmp_path / ".restart_notify.json"
    marker.write_text('{}', encoding="utf-8")
    os.utime(marker, (1000 - age, 1000 - age))
    gap = 600 if age == 600 else 300
    assert guard.planned_marker_is_recent(marker, now=1000, max_gap_seconds=gap, use_mtime=True) is expected


def test_planned_restart_does_not_spend_boot_budget(tmp_path, monkeypatch):
    state = tmp_path / "restart_loop.json"
    monkeypatch.setattr(guard, "_state_path", lambda: state)

    assert guard.check_and_record(max_restarts=3, now=100.0) is False
    assert guard.check_and_record(max_restarts=3, now=110.0, planned_restart=True) is False
    assert guard._load_boots() == [100.0]


def test_alternating_planned_and_crash_boots_trip_within_one_chain(tmp_path, monkeypatch):
    state = tmp_path / "restart_loop.json"
    monkeypatch.setattr(guard, "_state_path", lambda: state)

    # Each kind is 400s apart, but every boot is 200s apart. The merged chain
    # must count the second planned restart instead of treating it as a new exemption.
    for now, planned_restart in ((0.0, True), (200.0, False), (400.0, True), (600.0, False)):
        tripped = guard.check_and_record(
            max_restarts=3,
            now=now,
            max_gap_seconds=300,
            planned_restart=planned_restart,
        )

    assert tripped is True


def test_tripped_chain_resumes_once_for_planned_restart_then_trips(tmp_path, monkeypatch):
    state = tmp_path / "restart_loop.json"
    monkeypatch.setattr(guard, "_state_path", lambda: state)

    assert [guard.check_and_record(max_restarts=3, now=now) for now in (100.0, 110.0, 120.0)] == [
        False, False, True,
    ]
    assert guard.check_and_record(max_restarts=3, now=130.0, planned_restart=True) is False
    assert guard.check_and_record(max_restarts=3, now=140.0, planned_restart=True) is True


def test_legacy_boots_only_state_is_still_counted(tmp_path, monkeypatch):
    state = tmp_path / "restart_loop.json"
    monkeypatch.setattr(guard, "_state_path", lambda: state)
    state.write_text('{"boots": [100.0, 110.0]}', encoding="utf-8")

    assert guard.check_and_record(max_restarts=3, now=120.0) is True


def test_planned_marker_must_be_recent_relative_to_boot(tmp_path):
    marker = tmp_path / ".restart_pending.json"
    marker.write_text('{"requested_at": 1000.0}', encoding="utf-8")

    assert guard.planned_marker_is_recent(marker, now=1100.0, max_gap_seconds=300) is True
    assert guard.planned_marker_is_recent(marker, now=1301.0, max_gap_seconds=300) is False
    marker.write_text('{"via_service": true}', encoding="utf-8")
    assert guard.planned_marker_is_recent(marker, now=1000.0) is False
    assert guard.planned_marker_is_recent(tmp_path / "missing.json", now=1000.0) is False


def test_repeated_planned_restart_loop_still_trips(tmp_path, monkeypatch):
    state = tmp_path / "restart_loop.json"
    monkeypatch.setattr(guard, "_state_path", lambda: state)

    assert guard.check_and_record(max_restarts=3, now=100.0) is False
    assert guard.check_and_record(max_restarts=3, now=110.0, planned_restart=True) is False
    assert guard.check_and_record(max_restarts=3, now=120.0, planned_restart=True) is False
    assert guard.check_and_record(max_restarts=3, now=130.0, planned_restart=True) is True


def test_startup_marks_pending_planned_restart_for_breaker(tmp_path, monkeypatch):
    class _Store:
        _entries = {}

        def _ensure_loaded_locked(self):
            return None

        class _Lock:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        _lock = _Lock()

    runner = object.__new__(GatewayStartupMixin)
    runner.session_store = _Store()
    runner._restart_loop_guard_config = lambda: (3, 60, 300)
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.setattr(guard, "_state_path", lambda: tmp_path / "gateway" / "restart_loop.json")
    (tmp_path / ".restart_notify.json").write_text('{}', encoding="utf-8")
    guard_call = Mock(return_value=False)
    monkeypatch.setattr(guard, "check_and_record", guard_call)

    assert runner._resume_pending_candidates() == []
    guard_call.assert_not_called()

    class _Entry:
        resume_pending = True
        suspended = False
        origin = object()
        resume_reason = "gateway_restart"

    _Store._entries = {"session": _Entry()}
    runner._AUTO_RESUME_REASONS = {"gateway_restart"}
    runner._restart_loop_guard_config = lambda: (3, 60, 300)
    assert runner._resume_pending_candidates() == [_Store._entries["session"]]
    guard_call.assert_called_once_with(3, 60, max_gap_seconds=300, planned_restart=True)
