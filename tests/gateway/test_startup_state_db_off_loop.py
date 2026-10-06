"""Startup regressions for state.db work on the gateway readiness path."""

import asyncio
import threading
import time

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_startup_recovery_snapshot_does_not_block_gateway_loop():
    """A blocking recovery read must run in a worker, not on the event-loop thread."""
    runner = object.__new__(GatewayRunner)
    started = threading.Event()

    def blocking_snapshot(*args, **kwargs):
        del args, kwargs
        started.set()
        time.sleep(0.2)
        return ["complete"]

    runner._resume_pending_candidates = blocking_snapshot
    recovery = asyncio.create_task(runner._resume_pending_candidates_async())
    await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=0.5)

    probe = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(probe.set_result, True)
    assert await asyncio.wait_for(probe, timeout=0.05)
    assert await recovery == ["complete"]


def test_gateway_constructor_does_not_open_or_maintain_state_db(monkeypatch, tmp_path):
    """Constructor setup must not contend on state.db before adapters can connect."""
    calls = []
    monkeypatch.setattr(
        GatewayRunner,
        "_open_session_db_for_active_scope",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "gateway.run._housekeeping_state_db_maintenance",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
        sessions_dir=tmp_path / "sessions",
    )

    GatewayRunner(config)

    assert calls == []
