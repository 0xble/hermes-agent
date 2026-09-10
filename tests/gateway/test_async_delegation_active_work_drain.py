"""Gateway drain coverage for live async-delegation workers.

Async delegations run on their own daemon executor, so an idle parent gateway
has no entry in ``_running_agents`` while a delegated child is still executing.
The shutdown drain must use the delegation registry rather than infer liveness
from the parent session.
"""

import asyncio
import threading
import time

import pytest

from tests.gateway.restart_test_helpers import make_restart_runner
from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def _clean_async_delegations():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    deadline = time.monotonic() + 2
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _dispatch_blocked_worker(started: threading.Event, release: threading.Event):
    def _runner():
        started.set()
        release.wait(timeout=10)
        return {"status": "completed", "summary": "finished"}

    result = ad.dispatch_async_delegation(
        goal="wait for gateway drain", context=None, toolsets=None, role="leaf", model="test",
        session_key="", runner=_runner, max_async_children=1,
    )
    assert result["status"] == "dispatched"


@pytest.mark.asyncio
async def test_native_gateway_drain_waits_for_live_async_delegation_with_idle_parent():
    runner, _adapter = make_restart_runner()
    started = threading.Event()
    release = threading.Event()
    _dispatch_blocked_worker(started, release)
    assert started.wait(timeout=2), "delegated worker did not start"
    assert runner._running_agents == {}

    drain = asyncio.create_task(runner._drain_active_agents(2.0))
    try:
        await asyncio.sleep(0.15)
        assert not drain.done(), "drain returned while the live delegated worker was blocked"
        assert runner._active_work_count() == 1
    finally:
        release.set()

    _snapshot, timed_out = await drain
    assert timed_out is False

    deadline = time.monotonic() + 2
    while ad.active_count() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert ad.active_count() == 0
    assert runner._active_work_count() == 0, "completed delegation must not keep the gateway busy"


def test_forced_gateway_interrupt_signals_live_async_delegation():
    runner, _adapter = make_restart_runner()
    started = threading.Event()
    release = threading.Event()
    interrupted = threading.Event()

    def _runner():
        started.set()
        release.wait(timeout=10)
        return {"status": "interrupted", "summary": None}

    def _interrupt():
        interrupted.set()
        release.set()

    result = ad.dispatch_async_delegation(
        goal="interrupt at forced stop", context=None, toolsets=None, role="leaf", model="test",
        session_key="", runner=_runner, interrupt_fn=_interrupt, max_async_children=1,
    )
    assert result["status"] == "dispatched"
    assert started.wait(timeout=2), "delegated worker did not start"

    runner._interrupt_running_agents("gateway forced stop")

    assert interrupted.wait(timeout=2), "forced shutdown did not signal the live delegated worker"
