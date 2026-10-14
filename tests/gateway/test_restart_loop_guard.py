"""Restart-loop breaker behavior for planned and unplanned predecessor exits."""

import asyncio
import os
import signal
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


@pytest.mark.asyncio
async def test_chat_restart_after_long_drain_exempts_next_boot(tmp_path, monkeypatch):
    from gateway import run, status
    from gateway.platforms.base import MessageEvent
    from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
    from gateway.run_shutdown import GatewayShutdownMixin
    from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setattr(guard, "_state_path", lambda: tmp_path / "gateway" / "restart_loop.json")
    clock = [1000.0]
    monkeypatch.setattr(guard.time, "time", lambda: clock[0])
    monkeypatch.setattr("gateway.restart.is_gateway_supervisor_process", lambda: True)
    monkeypatch.setattr(status, "remove_pid_file", lambda: None)
    monkeypatch.setattr(status, "release_gateway_runtime_lock", lambda: None)
    monkeypatch.setattr(status, "flush_runtime_status_async", AsyncMock(return_value=True))
    monkeypatch.setattr(run, "_shutdown_gateway_health_export", lambda *_: None)
    predecessor, _ = make_restart_runner()
    predecessor._is_stale_restart_redelivery = lambda _: False
    predecessor._running_agent_count = lambda: 1
    predecessor._mark_api_runs_shutdown_requested = lambda: None

    async def long_drain():
        # /restart can wait up to 1800s before stop(), longer than the 300s chain gap.
        clock[0] += 601
        return False

    async def finish_shutdown(**kwargs):
        assert kwargs == {"restart": True, "detached_restart": False, "service_restart": True}
        ctx = SimpleNamespace(timed_out=True, active_agents={}, elapsed=lambda: 0)
        await GatewayShutdownMixin._stop_persist_exit_state(predecessor, ctx)

    predecessor._await_active_work_before_restart = long_drain
    predecessor.stop = finish_shutdown
    await predecessor._handle_restart_command(MessageEvent(text="/restart", source=make_restart_source()))
    marker = tmp_path / ".restart_notify.json"
    os.utime(marker, (clock[0], clock[0]))
    await predecessor._restart_task
    assert clock[0] - marker.stat().st_mtime > guard.DEFAULT_MAX_GAP_SECONDS
    assert predecessor._exit_code == GATEWAY_SERVICE_RESTART_EXIT_CODE
    assert _resume_runner()._resume_pending_candidates()
    assert guard._load_state() == ([], [clock[0]])


@pytest.mark.parametrize("age, planned", [(0, True), (301, False)])
def test_restart_notification_fallback_requires_recent_marker(tmp_path, monkeypatch, age, planned):
    from gateway import run

    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setattr(guard, "_state_path", lambda: tmp_path / "gateway" / "restart_loop.json")
    now = time.time()
    monkeypatch.setattr(guard.time, "time", lambda: now)
    marker = tmp_path / ".restart_notify.json"
    marker.write_text('{"platform": "telegram", "chat_id": "123"}', encoding="utf-8")
    os.utime(marker, (now - age, now - age))

    assert _resume_runner()._resume_pending_candidates()
    assert guard._load_state() == (([], [now]) if planned else ([now], []))


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


@pytest.mark.parametrize("contents", ['{"requested_at": 699}', '{"requested_at": true}', '{}', '[]', '{broken'])
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


@pytest.mark.parametrize("use_mtime", [False, True])
def test_planned_marker_uses_configured_chain_gap(tmp_path, use_mtime):
    marker = tmp_path / ".restart_pending.json"
    marker.write_text('{"requested_at": 1000.0}', encoding="utf-8")
    os.utime(marker, (1000, 1000))

    assert guard.planned_marker_is_recent(marker, now=1300, use_mtime=use_mtime) is True
    assert guard.planned_marker_is_recent(marker, now=1301, use_mtime=use_mtime) is False
    assert guard.planned_marker_is_recent(
        marker, now=1600, max_gap_seconds=600, use_mtime=use_mtime,
    ) is True
    assert guard.planned_marker_is_recent(tmp_path / "missing.json", now=1000, use_mtime=use_mtime) is False


def test_repeated_planned_restart_loop_still_trips(tmp_path, monkeypatch):
    state = tmp_path / "restart_loop.json"
    monkeypatch.setattr(guard, "_state_path", lambda: state)

    assert guard.check_and_record(max_restarts=3, now=100.0) is False
    assert guard.check_and_record(max_restarts=3, now=110.0, planned_restart=True) is False
    assert guard._load_state() == ([100.0], [110.0])
    assert guard.check_and_record(max_restarts=3, now=120.0, planned_restart=True) is False
    assert guard.check_and_record(max_restarts=3, now=130.0, planned_restart=True) is True
