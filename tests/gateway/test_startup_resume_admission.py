"""A restart resumes every interrupted session; their turn openings must not all start at once.

Each resumed turn takes one of ``_STARTUP_RESUME_CONCURRENCY`` slots until it ends or its opening
window passes, so the boot herd cannot saturate the shared state.db writer by itself, and a long
resumed turn never holds later sessions for its whole run.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gateway import run_startup
from gateway.run import GatewayRunner


class _Adapter:
    def __init__(self, release: asyncio.Event):
        self._session_tasks = {}
        self.release = release
        self.running = 0
        self.peak = 0
        self.started = []

    async def handle_message(self, event):
        key = event.session_key
        self.started.append(key)

        async def turn():
            self.running += 1
            self.peak = max(self.peak, self.running)
            try:
                await self.release.wait()
            finally:
                self.running -= 1

        self._session_tasks[key] = asyncio.create_task(turn())


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._peek_session_state = lambda key: None
    return runner


@pytest.mark.asyncio
async def test_resumed_turn_openings_are_admitted_a_few_at_a_time(monkeypatch):
    monkeypatch.setattr(run_startup, "_STARTUP_RESUME_OPENING_S", 30.0)
    release = asyncio.Event()
    adapter, runner = _Adapter(release), _runner()
    total = run_startup._STARTUP_RESUME_CONCURRENCY + 3
    tasks = [asyncio.create_task(runner._run_startup_resume_event(
        adapter, SimpleNamespace(session_key=f"s{i}"), f"s{i}")) for i in range(total)]
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(adapter.started) == run_startup._STARTUP_RESUME_CONCURRENCY
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert adapter.started == [f"s{i}" for i in range(total)]  # FIFO admission
    assert adapter.peak <= run_startup._STARTUP_RESUME_CONCURRENCY


@pytest.mark.asyncio
async def test_long_resumed_turn_releases_its_slot_after_the_opening_window(monkeypatch):
    monkeypatch.setattr(run_startup, "_STARTUP_RESUME_OPENING_S", 0.05)
    release = asyncio.Event()
    adapter, runner = _Adapter(release), _runner()
    total = run_startup._STARTUP_RESUME_CONCURRENCY + 3
    tasks = [asyncio.create_task(runner._run_startup_resume_event(
        adapter, SimpleNamespace(session_key=f"s{i}"), f"s{i}")) for i in range(total)]
    await asyncio.sleep(0.5)
    # Every session opened although no turn has finished; each caller still waits for its turn.
    assert len(adapter.started) == total
    assert not any(task.done() for task in tasks)
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 5)
