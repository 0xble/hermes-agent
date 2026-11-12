"""The live bootstrap handler honors restart intent without changing stop precedence."""

import asyncio
import os
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import restart_loop_guard, run, run_bootstrap, shutdown_forensics, status


@pytest.fixture
def signal_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(shutdown_forensics, "snapshot_shutdown_context", lambda *_: None)
    runner = SimpleNamespace(stop=AsyncMock(), _signal_initiated_shutdown=False)
    tasks = []
    create_task = asyncio.create_task

    def schedule(coro):
        task = create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(run.asyncio, "create_task", schedule)
    return runner, tasks


@pytest.mark.asyncio
async def test_live_sigterm_consumes_restart_intent(signal_runner):
    runner, tasks = signal_runner
    assert status.write_planned_restart_marker(os.getpid())
    unplanned = [False]

    run_bootstrap._start_gateway_make_shutdown_signal_handler(runner, unplanned)(signal.SIGTERM)
    await asyncio.gather(*tasks)

    runner.stop.assert_awaited_once_with(restart=True, service_restart=True)
    assert not unplanned[0] and not runner._signal_initiated_shutdown
    assert runner._stop_requested_by_signal
    assert not status._get_planned_restart_marker_path().exists()
    assert restart_loop_guard.consume_planned_exit()
    assert not restart_loop_guard.consume_planned_exit()


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["unplanned", "takeover", "stop", "interrupt"])
async def test_live_signal_preserves_stop_precedence(signal_runner, monkeypatch, intent):
    runner, tasks = signal_runner
    monkeypatch.setattr(status, "consume_takeover_marker_for_self", lambda: intent == "takeover")
    monkeypatch.setattr(status, "consume_planned_stop_marker_for_self", lambda: intent == "stop")
    if intent != "unplanned":
        assert status.write_planned_restart_marker(os.getpid())
    unplanned = [False]
    received_signal = signal.SIGINT if intent == "interrupt" else signal.SIGTERM

    run_bootstrap._start_gateway_make_shutdown_signal_handler(runner, unplanned)(received_signal)
    await asyncio.gather(*tasks)

    runner.stop.assert_awaited_once_with()
    assert unplanned[0] == runner._signal_initiated_shutdown == (intent == "unplanned")
    assert getattr(runner, "_stop_requested_by_signal", False) == (intent != "takeover")
    assert status._get_planned_restart_marker_path().exists() == (intent != "unplanned")
