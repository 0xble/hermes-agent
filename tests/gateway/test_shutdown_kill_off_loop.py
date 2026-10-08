"""Gateway shutdown must not run process teardown on the event loop (#116327).

``ProcessRegistry.kill_all()`` is synchronous and blocking (per-target
``kill_process`` does disk I/O via ``_write_checkpoint`` and may spawn
``subprocess.run`` up to 15 s via ``_stop_systemd_unit``). Driving it inline
from ``_stop_interrupt_remaining_work`` monopolizes the asyncio loop. These
tests pin the invariant: the kill sweep runs off-loop, in phase order.
"""

import asyncio
import os
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.run_shutdown import GatewayShutdownMixin
from tests.gateway.restart_test_helpers import make_restart_runner


def _make_phase_runner(monkeypatch, events):
    runner, _adapter = make_restart_runner()
    runner._restart_drain_timeout = 0.01

    loop_thread = threading.current_thread()

    def _fake_kill_all(task_id=None, **kwargs):
        # kwargs carry kill_all's keyword-only args (source, consume_output);
        # the shutdown sweep passes source="gateway_shutdown" so a
        # persist_on_release job (#41225) is still reached on host exit.
        assert kwargs.get("source") == "gateway_shutdown", kwargs
        events.append(("kill_all", threading.current_thread()))
        return 2

    import tools.process_registry as _pr

    monkeypatch.setattr(_pr.process_registry, "kill_all", _fake_kill_all)
    monkeypatch.setattr(
        "cron.scheduler.mark_running_jobs_interrupted", lambda *a, **k: []
    )
    monkeypatch.setattr("tools.async_delegation.interrupt_all", lambda *a, **k: 0)
    monkeypatch.setattr(
        "tools.terminal_tool_lifecycle.cleanup_all_environments",
        lambda: events.append(("cleanup_envs", threading.current_thread())),
    )
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle.cleanup_all_browsers",
        lambda: events.append(("cleanup_browsers", threading.current_thread())),
    )
    return runner, loop_thread


def _make_ctx():
    ctx = GatewayShutdownMixin._StopContext(deferred_count=lambda: 0)
    ctx.started_at = time.monotonic()
    return ctx


@pytest.mark.asyncio
async def test_post_interrupt_kill_runs_off_event_loop(monkeypatch):
    """kill_all must execute on a worker thread, not the loop thread (#116327)."""
    events: list = []
    runner, loop_thread = _make_phase_runner(monkeypatch, events)

    await runner._stop_interrupt_remaining_work(_make_ctx())

    kill_threads = [t for name, t in events if name == "kill_all"]
    assert kill_threads, f"expected kill_all to run, got events: {events}"
    for t in kill_threads:
        assert t is not loop_thread, (
            "kill_all ran on the event-loop thread; it must be offloaded "
            "via asyncio.to_thread (#116327)"
        )


@pytest.mark.asyncio
async def test_graceful_kill_uses_parent_first_registry_path(monkeypatch):
    """An unbounded stop must not opt the registry into the bounded sweep."""
    import tools.process_registry as _pr

    observed = {}

    def _fake_kill_all(**kwargs):
        observed.update(kwargs)
        return 1

    monkeypatch.setattr(_pr.process_registry, "kill_all", _fake_kill_all)

    assert await GatewayShutdownMixin._stop_kill_tool_subprocesses_off_loop("graceful") == []
    assert "deadline" not in observed
    assert "stop_event" not in observed


def test_foreground_processes_are_killed_after_shared_deadline(monkeypatch):
    """The fast foreground-process sweep is unconditional after kill_all expires."""
    events = []
    runner, _loop_thread = _make_phase_runner(monkeypatch, events)
    import tools.environments.base as _base

    monkeypatch.setattr(_base, "kill_live_foreground_processes", lambda **_kw: events.append(("foreground", None)))
    stop_event = threading.Event()
    stop_event.set()

    GatewayShutdownMixin._stop_kill_tool_subprocesses(
        "expired", deadline=time.monotonic() - 1.0, stop_event=stop_event,
    )

    assert [name for name, _thread in events] == ["foreground"]


@pytest.mark.asyncio
async def test_mark_running_cron_jobs_runs_off_event_loop(monkeypatch):
    """The cron jobs-store write must not block the shutdown event loop."""
    events = []
    runner, loop_thread = _make_phase_runner(monkeypatch, events)

    monkeypatch.setattr(
        "cron.scheduler.mark_running_jobs_interrupted",
        lambda *args, **kwargs: events.append(("mark_cron", threading.current_thread())) or [],
    )
    await runner._stop_interrupt_remaining_work(_make_ctx())

    mark_threads = [thread for name, thread in events if name == "mark_cron"]
    assert mark_threads
    assert all(thread is not loop_thread for thread in mark_threads)


@pytest.mark.asyncio
async def test_restart_mark_running_cron_jobs_is_bounded(monkeypatch):
    """A held cron fire fence cannot consume the whole bounded restart handoff."""
    events: list = []
    runner, _loop_thread = _make_phase_runner(monkeypatch, events)
    runner._restart_requested = True
    runner._restart_shutdown_bound = lambda: 0.05

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def _blocked_mark(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=5)
        finished.set()
        return ["late-job"]

    monkeypatch.setattr("cron.scheduler.mark_running_jobs_interrupted", _blocked_mark)

    started = time.monotonic()
    await runner._stop_interrupt_remaining_work(_make_ctx())
    elapsed = time.monotonic() - started

    try:
        assert entered.wait(timeout=0.2)
        assert elapsed < 0.5
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(timeout=1)


@pytest.mark.asyncio
async def test_nonrestart_mark_running_cron_jobs_is_bounded(monkeypatch):
    """A held cron fire fence cannot hang ordinary shutdown teardown."""
    events: list = []
    runner, _loop_thread = _make_phase_runner(monkeypatch, events)
    runner._restart_requested = False
    runner._signal_initiated_shutdown = True
    runner._signal_interrupt_grace_timeout = 0.0

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def _blocked_mark(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=8)
        finished.set()
        return ["late-job"]

    monkeypatch.setattr("cron.scheduler.mark_running_jobs_interrupted", _blocked_mark)
    started = time.monotonic()
    operation = asyncio.create_task(runner._stop_interrupt_remaining_work(_make_ctx()))
    # Bound = post-interrupt grace (0 here) + the existing 2s cooperative sweep bound, which
    # also reaches the same blocked marker. The old code awaited the marker until release (8s).
    done, _pending = await asyncio.wait({operation}, timeout=3.0)
    elapsed = time.monotonic() - started

    try:
        assert operation in done
        assert elapsed < 3.0
        assert entered.wait(timeout=0.2)
        assert not finished.is_set()
    finally:
        release.set()
        await asyncio.wait({operation}, timeout=0.5)
        assert finished.wait(timeout=1)


@pytest.mark.asyncio
async def test_interrupt_phase_captures_agent_admitted_after_drain_snapshot(monkeypatch):
    """Late admission is finalized/flushed instead of being cleared as unowned state."""
    events: list = []
    runner, _loop_thread = _make_phase_runner(monkeypatch, events)
    runner._restart_requested = False
    runner._post_interrupt_grace_timeout = lambda: 0.0
    late_agent = MagicMock()
    runner._running_agents = {"late-session": late_agent}
    monkeypatch.setattr(
        GatewayShutdownMixin,
        "_mark_running_sessions_resume_pending",
        AsyncMock(return_value=[]),
    )
    ctx = _make_ctx()
    ctx.active_agents = {}

    await runner._stop_interrupt_remaining_work(ctx)

    assert ctx.active_agents["late-session"] is late_agent


@pytest.mark.asyncio
async def test_post_interrupt_kill_preserves_phase_order(monkeypatch):
    """Offloading must not reorder the teardown sequence within the phase."""
    events: list = []
    runner, _loop_thread = _make_phase_runner(monkeypatch, events)

    await runner._stop_interrupt_remaining_work(_make_ctx())

    names = [name for name, _t in events]
    assert "kill_all" in names
    assert names.index("kill_all") < names.index("cleanup_envs")
    assert names.index("cleanup_envs") < names.index("cleanup_browsers")
    # The loop must still be responsive while the sweep runs: a callback
    # scheduled during teardown fires without waiting for the phase.
    probe = asyncio.Event()
    asyncio.get_running_loop().call_soon(probe.set)
    await asyncio.wait_for(probe.wait(), timeout=5)


@pytest.mark.asyncio
async def test_blocking_kill_sweep_is_detached_without_late_registry_cleanup(monkeypatch):
    """A stuck kill worker cannot hold restart or mutate the registry after its deadline."""
    events: list[tuple[str, float]] = []
    runner, _loop_thread = _make_phase_runner(monkeypatch, events)
    import tools.process_registry as _pr

    def _blocking_kill_all(**kwargs):
        deadline = kwargs["deadline"]
        stop_event = kwargs["stop_event"]
        events.append(("kill_started", time.monotonic()))
        # Model a kill_all implementation blocked in registry/systemd I/O. It is
        # cooperative at the shutdown boundary, so no later target/checkpoint can run.
        while time.monotonic() < deadline + 30:
            if stop_event.is_set():
                events.append(("kill_stopped", time.monotonic()))
                return 0
            time.sleep(0.01)
        events.append(("late_kill", time.monotonic()))
        return 0

    monkeypatch.setattr(_pr.process_registry, "kill_all", _blocking_kill_all)
    started = time.monotonic()
    await runner._stop_kill_tool_subprocesses_off_loop("post-interrupt", timeout=0.1)
    elapsed = time.monotonic() - started
    await asyncio.sleep(0.2)  # let the detached worker observe stop_event

    assert elapsed < 0.5
    assert [name for name, _when in events] == ["kill_started", "kill_stopped"]


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX sleep")
def test_real_kill_all_stop_event_kills_spawned_process():
    """The sweep's stop_event must not leak into kill_process()."""
    from tools.process_registry import ProcessRegistry

    registry = ProcessRegistry()
    session = registry.spawn_local("exec sleep 30", task_id="shutdown-test", owner_task_id="shutdown-test")
    try:
        killed = registry.kill_all(
            source="gateway_shutdown",
            deadline=time.monotonic() + 2.0,
            stop_event=threading.Event(),
        )
        assert killed == 1
        assert session.process is not None
        assert session.process.returncode is not None
    finally:
        registry.kill_all(source="test-cleanup")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="uses POSIX sleep")
async def test_real_shutdown_kill_sweep_kills_spawned_process(monkeypatch):
    """The off-loop shutdown path must exercise the real registry kill path."""
    import tools.process_registry as _pr
    from tools.process_registry import ProcessRegistry

    registry = ProcessRegistry()
    session = registry.spawn_local("exec sleep 30", task_id="shutdown-test", owner_task_id="shutdown-test")
    monkeypatch.setattr(_pr, "process_registry", registry)
    monkeypatch.setattr("cron.scheduler.mark_running_jobs_interrupted", lambda *a, **k: [])
    monkeypatch.setattr("tools.async_delegation.interrupt_all", lambda *a, **k: 0)
    monkeypatch.setattr("tools.terminal_tool_lifecycle.cleanup_all_environments", lambda: None)
    monkeypatch.setattr("tools.browser_tool_lifecycle.cleanup_all_browsers", lambda: None)
    try:
        await GatewayShutdownMixin._stop_kill_tool_subprocesses_off_loop("test", timeout=2.0)
        assert session.process is not None
        deadline = time.monotonic() + 6.0
        while session.process.returncode is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert session.process.returncode is not None
    finally:
        registry.kill_all(source="test-cleanup")
