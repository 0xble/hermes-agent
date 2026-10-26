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


@pytest.mark.asyncio
async def test_replay_scheduler_restarts_workers_after_idle():
    scheduler = ReplayScheduler(1)

    async def work():
        return "ok"

    scheduler.start()
    for _ in range(2):
        handle = scheduler.enqueue(priority=1, kind="human", session_key="s", dispatch=work)
        assert await scheduler.wait_for(handle) == "ok"
        await scheduler.wait_idle()
        await asyncio.sleep(0)  # Allow the worker's completion callback to retire it.
        assert not scheduler._workers
    await scheduler.close()


@pytest.mark.asyncio
async def test_gateway_replay_dispatch_and_drop_share_captured_profile(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from contextvars import ContextVar
    from gateway.run import GatewayRunner

    current = ContextVar("replay_test_home", default=None)
    seen = []

    @asynccontextmanager
    async def scope(home):
        token = current.set(home)
        try:
            yield
        finally:
            current.reset(token)

    monkeypatch.setattr("gateway.run._async_profile_runtime_scope", scope)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True, restart_replay_concurrency=1)
    scheduler = runner._get_replay_scheduler()

    async def dispatch():
        seen.append(("dispatch", current.get()))

    first = runner._enqueue_replay(
        priority=1, kind="human", session_key="s", dispatch=dispatch, profile_home=tmp_path,
    )
    scheduler.start()
    await scheduler.wait_for(first)
    await scheduler.wait_idle()
    # This synchronous enqueue cannot yield to a worker before close drops it.
    dropped = runner._enqueue_replay(
        priority=2, kind="resume", session_key="s", dispatch=dispatch, profile_home=tmp_path,
        on_drop=lambda: seen.append(("drop", current.get())),
    )
    await scheduler.close()
    assert dropped.future.cancelled()
    assert seen == [("dispatch", tmp_path), ("drop", tmp_path)]
    assert current.get() is None


@pytest.mark.asyncio
async def test_gated_resume_waits_for_room_until_a_person_waits_on_its_session():
    from gateway.replay_scheduler import BackgroundTurnGate

    running = {"busy"}
    scheduler = ReplayScheduler(2, gate=BackgroundTurnGate(1, lambda: running, spacing_s=0))
    started = []

    async def work(name):
        started.append(name)
        return name

    scheduler.start()
    blocked = scheduler.enqueue(priority=2, kind="resume", session_key="s1", dispatch=lambda: work("s1"), gated=True)
    other = scheduler.enqueue(priority=2, kind="resume", session_key="s2", dispatch=lambda: work("s2"), gated=True)
    human = scheduler.enqueue(priority=1, kind="human", session_key="h", dispatch=lambda: work("human"))
    assert await asyncio.wait_for(scheduler.wait_for(human), 5) == "human"
    assert started == ["human"]  # typed input never waits on the gate

    assert scheduler.promote("s2")
    assert await asyncio.wait_for(scheduler.wait_for(other), 5) == "s2"
    assert not blocked.future.done()

    running.clear()
    assert await asyncio.wait_for(scheduler.wait_for(blocked), 5) == "s1"
    await scheduler.close()


@pytest.mark.asyncio
async def test_draining_drops_queued_gated_items_without_starting_them():
    from gateway.replay_scheduler import BackgroundTurnGate

    draining = False
    gate = BackgroundTurnGate(1, lambda: {"busy"}, closing=lambda: draining, spacing_s=0)
    scheduler = ReplayScheduler(1, gate=gate)
    dropped, started = [], []

    async def work():
        started.append("resume")

    scheduler.start()
    resume = scheduler.enqueue(priority=2, kind="resume", session_key="s", dispatch=work,
                               on_drop=lambda: dropped.append("s"), gated=True)
    draining = True
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(scheduler.wait_for(resume), 5)
    # The drop callback releases the resume's preclaim so the drain does not wait on it.
    assert dropped == ["s"] and started == []
    await scheduler.close()
