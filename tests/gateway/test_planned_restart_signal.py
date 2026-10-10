"""A launchd bootout issued to reload the plist is a planned restart, not a crash.

``hermes update`` reloads the changed gateway plist with ``launchctl bootout``, which delivers a plain
SIGTERM. Read as an unplanned signal, it ran the unbounded post-interrupt tool sweep (36-53 s live)
and exited 1. The reloader marks the gateway pid first, so the SIGTERM takes the bounded restart
path a SIGUSR1 restart takes and exits with the service-restart code.
"""

import asyncio
import json
import os
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as run_mod
import gateway.shutdown_forensics as forensics
from gateway import status
from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
from gateway.run_shutdown import GatewayShutdownMixin, _resolve_gateway_exit_verdict
from tests.gateway.restart_test_helpers import make_restart_runner


async def _deliver_sigterm(monkeypatch):
    """Drive the real SIGTERM handler through a full stop with one agent that outlives the drain."""
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._exit_with_failure = False
    runner._restart_drain_timeout = 0.01
    runner._running_agents = {"stuck-session": MagicMock()}
    runner._post_interrupt_grace_timeout = lambda: 0.01
    sweeps = []

    async def _sweep(phase, *, timeout=None):
        sweeps.append((phase, timeout))
        return []

    monkeypatch.setattr(
        "gateway.run.GatewayRunner._stop_kill_tool_subprocesses_off_loop", staticmethod(_sweep)
    )
    monkeypatch.setattr(
        GatewayShutdownMixin, "_mark_running_sessions_resume_pending", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("cron.scheduler.mark_running_jobs_interrupted", lambda *a, **k: [])
    monkeypatch.setattr(forensics, "snapshot_shutdown_context", lambda *a, **k: None)
    created = []
    real_create_task = asyncio.create_task

    def _record(coro, **kwargs):
        task = real_create_task(coro, **kwargs)
        created.append(task)
        return task

    monkeypatch.setattr(run_mod.asyncio, "create_task", _record)
    signal_initiated = [False]
    with patch("gateway.status.remove_pid_file"), patch("gateway.status.publish_runtime_status"):
        run_mod._start_gateway_make_shutdown_signal_handler(runner, signal_initiated)(signal.SIGTERM)
        await asyncio.wait_for(created[0], timeout=10)
    return runner, sweeps, signal_initiated[0]


@pytest.mark.asyncio
async def test_sigterm_with_planned_restart_marker_takes_bounded_restart_path(monkeypatch):
    assert status.write_planned_restart_marker(os.getpid())

    runner, sweeps, signal_initiated = await _deliver_sigterm(monkeypatch)

    assert runner._restart_requested and runner._restart_via_service
    post_interrupt = [timeout for phase, timeout in sweeps if phase == "post-interrupt"]
    assert len(post_interrupt) == 1 and post_interrupt[0] is not None and post_interrupt[0] <= 2.0
    with pytest.raises(SystemExit) as exc:
        _resolve_gateway_exit_verdict(runner, signal_initiated)
    assert exc.value.code == GATEWAY_SERVICE_RESTART_EXIT_CODE
    # One-shot: a later SIGTERM is not a planned restart.
    assert status.consume_planned_restart_marker_for_self() is False


@pytest.mark.asyncio
async def test_sigterm_without_marker_keeps_signal_shutdown_exit_1(monkeypatch):
    runner, sweeps, signal_initiated = await _deliver_sigterm(monkeypatch)

    assert not runner._restart_requested
    assert ("post-interrupt", None) in sweeps
    assert signal_initiated is True
    # False => start_gateway's caller exits 1 so the service manager revives the gateway.
    assert _resolve_gateway_exit_verdict(runner, signal_initiated) is False


def test_marker_for_another_pid_is_ignored_and_stale_marker_cleared(monkeypatch):
    marker = status._get_planned_restart_marker_path()
    assert status.write_planned_restart_marker(os.getpid() + 1)
    assert status.consume_planned_restart_marker_for_self() is False
    assert not marker.exists()

    assert status.write_planned_restart_marker(os.getpid())
    record = json.loads(marker.read_text())
    record["written_at"] = "2000-01-01T00:00:00+00:00"
    marker.write_text(json.dumps(record))
    assert status.consume_planned_restart_marker_for_self() is False
    assert not marker.exists()
