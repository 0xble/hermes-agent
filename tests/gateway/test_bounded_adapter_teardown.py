"""Regression tests: the shutdown teardown loop must not hang on a wedged adapter.

`GatewayRunner._stop_impl()` tears down every adapter by awaiting
`cancel_background_tasks()` then `disconnect()`. Both calls can block
indefinitely when a platform's network state is half-dead (e.g. a wedged
Feishu/Lark WebSocket thread waiting on I/O). An unbounded await stalls the
whole shutdown past systemd's TimeoutStopSec; the resulting SIGKILL skips
atexit PID-file cleanup, so the next start dies with "PID file race lost"
(#14128).

The fix routes both teardown loops through `_bounded_adapter_teardown`,
which wraps each await in the existing per-adapter timeout budget
(HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT) and always returns.
"""

import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.run_shutdown import GatewayShutdownMixin


@pytest.fixture
def bare_runner():
    """A GatewayRunner shell that only needs _bounded_adapter_teardown."""
    return object.__new__(GatewayRunner)


@pytest.mark.asyncio
async def test_teardown_bounds_hanging_cancel(bare_runner, monkeypatch, caplog):
    """A wedged cancel_background_tasks() must time out, then disconnect runs."""
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.01")
    adapter = MagicMock()

    async def hang():
        await asyncio.sleep(0.2)

    adapter.cancel_background_tasks = AsyncMock(side_effect=hang)
    adapter.disconnect = AsyncMock(return_value=None)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await asyncio.wait_for(
            bare_runner._bounded_adapter_teardown(adapter, Platform.FEISHU),
            timeout=5.0,
        )

    # disconnect still attempted after the cancel timeout — forward progress.
    adapter.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_teardown_reuses_one_shared_deadline_after_cancel_consumes_budget(bare_runner, monkeypatch):
    """Disconnect gets only the shutdown time left after cancellation."""
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.05")
    adapter = MagicMock()

    async def hang():
        await asyncio.sleep(0.2)

    adapter.cancel_background_tasks = AsyncMock(side_effect=hang)
    adapter.disconnect = AsyncMock(side_effect=hang)
    started = asyncio.get_running_loop().time()
    await bare_runner._bounded_adapter_teardown(adapter, Platform.FEISHU)
    elapsed = asyncio.get_running_loop().time() - started

    # The old implementation spent a second full 50ms timeout in disconnect.
    assert elapsed < 0.075
    await asyncio.sleep(0)
    adapter.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_teardown_attempts_bounded_disconnect_after_expired_deadline(bare_runner, monkeypatch):
    """An expired outer deadline must not skip the adapter lock-release path."""
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.05")
    adapter = MagicMock()
    adapter.cancel_background_tasks = AsyncMock()
    adapter.disconnect = AsyncMock()

    await bare_runner._bounded_adapter_teardown(
        adapter, Platform.FEISHU, deadline=asyncio.get_running_loop().time() - 1.0,
    )
    await asyncio.sleep(0)

    adapter.cancel_background_tasks.assert_not_awaited()
    adapter.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_teardown_continues_after_cancellation_swallowing_background_cancel(
    bare_runner, monkeypatch
):
    """A stuck cancellation handler cannot prevent adapter disconnect.

    This models a platform task that catches ``CancelledError`` while it is
    unwinding.  The teardown deadline must release runner ownership promptly,
    then proceed to disconnect instead of waiting for that old task forever.
    """
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.01")
    adapter = MagicMock()
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def swallow_cancellation():
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        finished.set()

    adapter.cancel_background_tasks = AsyncMock(side_effect=swallow_cancellation)
    adapter.disconnect = AsyncMock(return_value=None)
    operation = asyncio.create_task(
        bare_runner._bounded_adapter_teardown(adapter, Platform.FEISHU)
    )
    await started.wait()
    done, _pending = await asyncio.wait({operation}, timeout=0.2)
    try:
        assert operation in done
        adapter.disconnect.assert_awaited_once()
    finally:
        release.set()
        await asyncio.wait({operation}, timeout=0.2)
        await asyncio.wait_for(finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_adapter_teardown_bounds_each_operation_without_skipping_disconnect(bare_runner, monkeypatch):
    """Several wedged adapters use parallel per-operation bounds, not an aggregate notice clock."""
    import time
    from gateway.run_shutdown import GatewayShutdownMixin

    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.01")
    bare_runner._restart_requested = False
    bare_runner._restart_detached = False
    async def hang_cancel():
        await asyncio.Event().wait()

    adapters = {}
    for platform in (Platform.TELEGRAM, Platform.FEISHU):
        adapter = MagicMock()
        adapter._pending_messages = {}
        adapter.cancel_background_tasks = AsyncMock(side_effect=hang_cancel)
        adapter.disconnect = AsyncMock()
        adapters[platform] = adapter
    bare_runner.adapters = adapters
    bare_runner._profile_adapters = {}
    bare_runner._agent_cache_lock = None
    bare_runner._agent_cache = None
    bare_runner._finalize_shutdown_agents = AsyncMock()
    bare_runner._cancel_process_completion_batch_tasks = AsyncMock()
    ctx = GatewayShutdownMixin._StopContext(deferred_count=lambda: 0, started_at=time.monotonic())
    start = time.monotonic()
    await asyncio.wait_for(bare_runner._stop_finalize_agents_and_adapters(ctx), timeout=2.0)
    assert time.monotonic() - start < 2.0
    for adapter in adapters.values():
        adapter.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_notice_budget_cannot_cancel_adapter_flush_or_disconnect(bare_runner, tmp_path, monkeypatch):
    """Pending adapter messages survive even when notices spent nearly the entire network budget."""
    import json
    import time
    from gateway.run_shutdown import GatewayShutdownMixin

    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir()
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    bare_runner._restart_requested = False
    bare_runner._restart_detached = False
    bare_runner._agent_cache_lock = None
    bare_runner._agent_cache = None
    bare_runner._finalize_shutdown_agents = AsyncMock()
    bare_runner._cancel_process_completion_batch_tasks = AsyncMock()
    adapter = MagicMock()
    adapter._pending_messages = {"agent:main:telegram:dm:1": "follow-up"}
    unwinding = asyncio.Event()

    async def cancel_background_tasks():
        # Cancellation during unwind used to skip the adapter's final synchronous flush.
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            unwinding.set()
            raise
        from gateway.shutdown_flush import flush_pending_to_file
        flush_pending_to_file(adapter._pending_messages, reason="adapter_shutdown")
        adapter._pending_messages.clear()

    adapter.cancel_background_tasks = AsyncMock(side_effect=cancel_background_tasks)
    adapter.disconnect = AsyncMock()
    bare_runner.adapters = {Platform.TELEGRAM: adapter}
    bare_runner._profile_adapters = {}
    ctx = GatewayShutdownMixin._StopContext(
        deferred_count=lambda: 0, started_at=time.monotonic(), notice_elapsed=2.95,
    )
    await asyncio.wait_for(bare_runner._stop_finalize_agents_and_adapters(ctx), timeout=2.0)
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in flush_dir.glob("*.json")]
    assert [payload["data"]["text"] for payload in payloads] == ["follow-up"]
    assert adapter._pending_messages == {}
    adapter.disconnect.assert_awaited_once()
    assert not unwinding.is_set()


@pytest.mark.asyncio
async def test_timed_out_restart_spools_followups_before_slow_cleanup(bare_runner, tmp_path, monkeypatch):
    """A cancelled finalization task cannot erase queued user input before it is durable."""
    import json
    import asyncio
    import time
    from gateway.run_shutdown import GatewayShutdownMixin

    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir()
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    monkeypatch.setattr("gateway.run_pending_recovery.get_routing_process_hermes_home", lambda: tmp_path)
    bare_runner._restart_requested = True
    bare_runner._restart_detached = False
    bare_runner.config = type("Config", (), {"multiplex_profiles": False})()
    bare_runner._primary_profile_name = "default"
    bare_runner._served_profile_homes = {"default": tmp_path}
    bare_runner._pending_messages = {}
    bare_runner._queued_events = {}
    bare_runner._profile_adapters = {}
    bare_runner._agent_cache_lock = None
    bare_runner._agent_cache = None
    adapter = MagicMock()
    adapter._pending_messages = {"agent:main:telegram:dm:1": "queued follow-up"}
    bare_runner.adapters = {Platform.TELEGRAM: adapter}

    async def slow_cleanup(*_args, **_kwargs):
        await asyncio.sleep(30)

    bare_runner._finalize_shutdown_agents = slow_cleanup
    ctx = GatewayShutdownMixin._StopContext(deferred_count=lambda: 0, started_at=time.monotonic())
    ctx.timed_out = True
    task = asyncio.create_task(bare_runner._stop_finalize_agents_and_adapters(ctx))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in flush_dir.glob("*.json")]
    assert [payload["data"]["text"] for payload in payloads] == ["queued follow-up"]
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_stop_impl_detaches_finalizer_that_swallow_cancellation(bare_runner, monkeypatch):
    """A finalizer that ignores cancellation cannot consume the restart handoff bound."""
    import time

    runner = bare_runner
    runner._restart_requested = True
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._restart_shutdown_bound = lambda: 0.2
    runner._restart_agent_finalize_bound = lambda _bound: 0.05
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._background_tasks = set()
    runner._stop_requested_by_signal = False
    release = asyncio.Event()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def begin(_self, ctx):
        ctx.started_at = time.monotonic()

    async def drain(_self, _timeout, ctx):
        ctx.timed_out = True
        ctx.active_agents = {}

    async def interrupt(_self, _ctx):
        return None

    async def finalizer(_self, _ctx, **_kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    async def release_state(_self, _ctx):
        return None

    def quiesce(_self, _timeout, _ctx):
        return None

    async def persist(_self, _ctx):
        return None

    monkeypatch.setattr(GatewayRunner, "_stop_begin_teardown", begin)
    monkeypatch.setattr(GatewayRunner, "_stop_drain_active_work", drain)
    monkeypatch.setattr(GatewayRunner, "_stop_interrupt_remaining_work", interrupt)
    monkeypatch.setattr(GatewayRunner, "_stop_finalize_agents_and_adapters", finalizer)
    monkeypatch.setattr(GatewayRunner, "_stop_release_runtime_state", release_state)
    monkeypatch.setattr(GatewayRunner, "_stop_quiesce_and_close_session_dbs", quiesce)
    monkeypatch.setattr(GatewayRunner, "_stop_persist_exit_state", persist)
    monkeypatch.setattr("gateway.run_shutdown._persist_shutdown_pending_messages", lambda _runner: 0)
    monkeypatch.setattr("gateway.run_shutdown.arm_shutdown_watchdog", lambda *args, **kwargs: None)

    operation = asyncio.create_task(GatewayRunner._stop_impl(runner))
    await started.wait()
    done, _pending = await asyncio.wait({operation}, timeout=0.5)
    try:
        assert operation in done
        assert cancelled.is_set()
    finally:
        release.set()
        await asyncio.wait({operation}, timeout=0.5)


@pytest.mark.asyncio
async def test_real_stop_impl_cancellation_during_idle_cleanup_still_disconnects(bare_runner, monkeypatch):
    """The production stop orchestration must reach disconnect after idle-cache cancellation."""
    import threading
    import time

    runner = bare_runner
    runner._restart_requested = True
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._restart_drain_timeout = 0.01
    runner._stop_requested_by_signal = False
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._profile_adapters = {}
    runner._startup_restore_queue = []
    runner._agent_cache_lock = threading.Lock()
    runner._agent_cache = {"idle:1": MagicMock()}
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.01")
    runner._restart_shutdown_bound = lambda: 0.5
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter.cancel_background_tasks = AsyncMock()
    adapter.disconnect = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}

    runner._finalize_shutdown_agents = AsyncMock()

    async def slow_idle_cleanup(*_args, **_kwargs):
        await asyncio.sleep(10)

    runner._cleanup_agent_resources_off_loop = slow_idle_cleanup

    async def begin(_self, ctx):
        ctx.started_at = time.monotonic()

    async def drain(_self, _timeout, ctx):
        ctx.active_agents = {"active": object()}
        ctx.timed_out = True

    async def interrupt(_self, _ctx):
        return None

    async def release(_self, _ctx):
        return None

    def quiesce(_self, _timeout, _ctx):
        return None

    async def persist(_self, _ctx):
        return None

    monkeypatch.setattr(GatewayRunner, "_stop_begin_teardown", begin)
    monkeypatch.setattr(GatewayRunner, "_stop_drain_active_work", drain)
    monkeypatch.setattr(GatewayRunner, "_stop_interrupt_remaining_work", interrupt)
    monkeypatch.setattr(GatewayRunner, "_stop_release_runtime_state", release)
    monkeypatch.setattr(GatewayRunner, "_stop_quiesce_and_close_session_dbs", quiesce)
    monkeypatch.setattr(GatewayRunner, "_stop_persist_exit_state", persist)

    await GatewayRunner._stop_impl(runner)

    adapter.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_timed_out_finalize_reserves_adapter_disconnect_budget(bare_runner):
    """A slow finalize hook cannot consume the restart teardown/disconnect slice."""
    import threading
    import time

    runner = bare_runner
    runner._restart_requested = True
    runner._restart_detached = False
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._profile_adapters = {}
    runner._startup_restore_queue = []
    runner._agent_cache_lock = threading.Lock()
    runner._agent_cache = {"idle:1": MagicMock()}
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter.cancel_background_tasks = AsyncMock()
    adapter.disconnect = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}

    async def slow_finalize(active, *, interrupted=False, stop_event=None, deadline=None):
        deadline = deadline or time.monotonic()
        await asyncio.sleep(max(0.0, deadline - time.monotonic()) + 0.05)

    runner._finalize_shutdown_agents = slow_finalize

    async def idle_cleanup(*_args, **_kwargs):
        await asyncio.sleep(2.0)

    runner._cleanup_agent_resources_off_loop = idle_cleanup
    ctx = GatewayShutdownMixin._StopContext(deferred_count=lambda: 0, started_at=time.monotonic())
    ctx.timed_out = True
    ctx.active_agents = {"s": object()}

    stop_event = threading.Event()
    bound = runner._restart_shutdown_bound()
    agent_bound = runner._restart_agent_finalize_bound(bound)
    started = time.monotonic()
    task = asyncio.create_task(GatewayRunner._stop_finalize_agents_and_adapters(
        runner, ctx, stop_event=stop_event, deadline=started + bound,
        agent_deadline=started + agent_bound,
    ))
    if not await GatewayRunner._wait_or_detach(task, agent_bound):
        stop_event.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    elapsed = time.monotonic() - started
    assert adapter.disconnect.await_count >= 1
    assert elapsed <= bound + 0.2



@pytest.mark.asyncio
async def test_zero_cleanup_timeout_means_unbounded(bare_runner):
    """timeout<=0 keeps its 'await unbounded' meaning; it is not an expired deadline."""
    finished = asyncio.Event()

    async def slow_cleanup():
        await asyncio.sleep(0.05)
        finished.set()

    assert await bare_runner._await_adapter_cleanup_with_timeout(slow_cleanup(), 0) is True
    assert finished.is_set()


@pytest.mark.asyncio
async def test_expired_deadline_signal_starts_cleanup_then_detaches(bare_runner):
    """An expired shared deadline is explicit: cleanup is started, then detached."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def wedged_cleanup():
        started.set()
        await release.wait()

    try:
        assert await bare_runner._await_adapter_cleanup_with_timeout(
            wedged_cleanup(), 0.0, deadline_expired=True,
        ) is False
        assert started.is_set()
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_teardown_env_zero_awaits_cancel_and_disconnect(bare_runner, monkeypatch):
    """HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT=0 must not skip cancel or detach disconnect."""
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0")
    adapter = MagicMock()
    done = []

    async def slow_cancel():
        await asyncio.sleep(0.02)
        done.append("cancel")

    async def slow_disconnect():
        await asyncio.sleep(0.02)
        done.append("disconnect")

    adapter.cancel_background_tasks = AsyncMock(side_effect=slow_cancel)
    adapter.disconnect = AsyncMock(side_effect=slow_disconnect)

    await bare_runner._bounded_adapter_teardown(adapter, Platform.FEISHU)

    assert done == ["cancel", "disconnect"]


@pytest.mark.asyncio
async def test_teardown_env_zero_still_honours_expired_shared_deadline(bare_runner, monkeypatch):
    """With an unbounded per-op budget, an expired restart deadline still bounds disconnect."""
    import time

    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0")
    adapter = MagicMock()
    release = asyncio.Event()

    async def wedged():
        await release.wait()

    adapter.cancel_background_tasks = AsyncMock(side_effect=wedged)
    adapter.disconnect = AsyncMock(side_effect=wedged)
    try:
        await asyncio.wait_for(
            bare_runner._bounded_adapter_teardown(
                adapter, Platform.FEISHU, deadline=time.monotonic() - 1.0,
            ),
            timeout=1.0,
        )
        adapter.cancel_background_tasks.assert_not_awaited()
        adapter.disconnect.assert_awaited_once()
    finally:
        release.set()
        await asyncio.sleep(0)
