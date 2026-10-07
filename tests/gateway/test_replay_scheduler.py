"""Phase-1 replay admission invariants."""

import asyncio

import pytest

from gateway.replay_scheduler import ReplayScheduler
from gateway.config import GatewayConfig


@pytest.mark.asyncio
async def test_replay_scheduler_caps_workers_and_preserves_priority_fifo():
    scheduler = ReplayScheduler(2)
    active = 0
    maximum = 0
    started = []
    release = asyncio.Event()

    async def work(name):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        started.append(name)
        await release.wait()
        active -= 1
        return name

    scheduler.start()
    handles = [
        scheduler.enqueue(priority=2, kind="resume", session_key=f"r{i}", dispatch=lambda i=i: work(f"r{i}"))
        for i in range(4)
    ]
    handles += [
        scheduler.enqueue(priority=1, kind="human", session_key=f"h{i}", dispatch=lambda i=i: work(f"h{i}"))
        for i in range(3)
    ]
    await asyncio.sleep(0)
    assert started == ["h0", "h1"]
    assert maximum == 2
    release.set()
    assert await asyncio.gather(*(scheduler.wait_for(handle) for handle in handles)) == ["r0", "r1", "r2", "r3", "h0", "h1", "h2"]
    await scheduler.close()


@pytest.mark.asyncio
async def test_replay_scheduler_failure_releases_slot():
    scheduler = ReplayScheduler(1)
    failed = asyncio.Event()

    async def fail():
        failed.set()
        raise RuntimeError("replay failed")

    async def succeed():
        return "ok"

    scheduler.start()
    first = scheduler.enqueue(priority=2, kind="resume", session_key="bad", dispatch=fail)
    second = scheduler.enqueue(priority=2, kind="resume", session_key="good", dispatch=succeed)
    await failed.wait()
    with pytest.raises(RuntimeError, match="replay failed"):
        await scheduler.wait_for(first)
    assert await scheduler.wait_for(second) == "ok"
    assert scheduler.active_count == 0
    await scheduler.close()


@pytest.mark.asyncio
async def test_replay_scheduler_orders_humans_then_resumes_then_followups():
    scheduler = ReplayScheduler(1)
    started = []
    release = asyncio.Event()

    async def work(name):
        started.append(name)
        await release.wait()
        return name

    scheduler.start()
    human = scheduler.enqueue(priority=1, kind="human", session_key="h", dispatch=lambda: work("human"))
    resume = scheduler.enqueue(priority=2, kind="resume", session_key="r", dispatch=lambda: work("resume"))
    followup = scheduler.enqueue(priority=3, kind="followup", session_key="f", dispatch=lambda: work("followup"))
    await asyncio.sleep(0)
    assert started == ["human"]
    release.set()
    assert await asyncio.gather(
        scheduler.wait_for(human), scheduler.wait_for(resume), scheduler.wait_for(followup)
    ) == ["human", "resume", "followup"]
    assert started == ["human", "resume", "followup"]
    await scheduler.close()


@pytest.mark.asyncio
async def test_replay_scheduler_close_drops_queued_and_bounds_active_work():
    scheduler = ReplayScheduler(1)
    started = asyncio.Event()
    dropped = []
    release = asyncio.Event()

    async def active_work():
        started.set()
        await release.wait()

    active = scheduler.enqueue(
        priority=2, kind="resume", session_key="active", dispatch=active_work,
    )
    queued = scheduler.enqueue(
        priority=2, kind="resume", session_key="queued", dispatch=active_work,
        on_drop=lambda: dropped.append("queued"),
    )
    scheduler.start()
    await started.wait()
    await scheduler.close(timeout=0.01)

    assert dropped == ["queued"]
    assert queued.future.cancelled()
    assert active.future.cancelled()
    assert scheduler.queue_depth == 0
    assert scheduler.active_count == 0


def test_restart_replay_concurrency_config_parses_and_defaults():
    assert GatewayConfig.from_dict({"restart_replay_concurrency": 5}).restart_replay_concurrency == 5
    assert GatewayConfig.from_dict({"restart_replay_concurrency": 0}).restart_replay_concurrency == 2
    assert GatewayConfig.from_dict({"restart_replay_concurrency": "bad"}).restart_replay_concurrency == 2
