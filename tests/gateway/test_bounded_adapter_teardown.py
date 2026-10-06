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
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner


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

