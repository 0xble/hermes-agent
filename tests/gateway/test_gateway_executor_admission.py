"""Bounded gateway admission through the real executor and turn entry point."""

import asyncio
import contextvars
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.config import Platform
from gateway.turn_context import TurnContext


def _runner():
    runner = object.__new__(GatewayRunner)
    runner._executor_lock = threading.Lock()
    runner._executor = None
    runner._executor_closing = False
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("full", [False, True])
async def test_turn_admission_reports_queue_or_overload_without_starting(full, monkeypatch):
    runner = _runner()
    executor = runner._get_executor()
    release = threading.Event()
    barrier = threading.Barrier(executor._max_workers + 1)
    adapter = SimpleNamespace(send=AsyncMock())
    monkeypatch.setattr(runner, "_adapter_for_source", lambda source: adapter)
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")
    marker = contextvars.ContextVar("admission_test", default="missing")
    token = marker.set("preserved")
    ran = []

    def block():
        barrier.wait(timeout=10)
        release.wait(timeout=10)

    futures = [executor.submit(block) for _ in range(executor._max_workers)]
    barrier.wait(timeout=10)
    if full:
        futures.extend(executor.submit(lambda: None) for _ in range(executor._max_workers))
    ctx = TurnContext(
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="test-chat"),
        session_key="test-session", session_id="test-session",
    )
    try:
        worker = runner._run_agent_start_turn_worker(
            ctx, lambda: ran.append(marker.get()) or {"final_response": "finished"},
        )
        # The executor coroutine reaches its first suspension: admission/notice has run.
        await asyncio.sleep(0)
        assert not ran
        if full:
            result = await asyncio.wait_for(worker.executor_task, timeout=5)
            assert result["failed"] is True
            assert "not started" in result["final_response"]
            assert worker.worker_done.is_set()
        else:
            adapter.send.assert_awaited_once()
            assert "queued" in adapter.send.call_args.args[1].lower()
            assert adapter.send.call_args.kwargs["metadata"]["_interim_send"] is True
            release.set()
            result = await asyncio.wait_for(worker.executor_task, timeout=5)
            assert result["final_response"] == "finished"
            assert ran == ["preserved"]
    finally:
        release.set()
        for future in futures:
            future.result(timeout=10)
        runner._shutdown_executor(drain_timeout=10)
        marker.reset(token)


def test_cancelled_queued_work_keeps_admission_until_dequeued():
    runner = _runner()
    executor = runner._get_executor()
    release = threading.Event()
    barrier = threading.Barrier(executor._max_workers + 1)

    def block():
        barrier.wait(timeout=10)
        release.wait(timeout=10)

    running = [executor.submit(block) for _ in range(executor._max_workers)]
    barrier.wait(timeout=10)
    ran = []
    queued = [executor.submit(lambda: ran.append(True)) for _ in range(executor._max_workers)]
    try:
        assert all(future.cancel() for future in queued)
        with pytest.raises(RuntimeError, match="Gateway is busy"):
            executor.submit(lambda: None)
    finally:
        release.set()
        executor.shutdown(wait=True)
    assert not ran
    assert all(future.done() for future in running + queued)


@pytest.mark.asyncio
async def test_cancelling_running_waiter_does_not_free_capacity():
    from gateway.run_executor import GatewayExecutor, run_gateway_work

    executor = GatewayExecutor(max_workers=1)
    started = threading.Event()
    release = threading.Event()

    def block():
        started.set()
        release.wait(timeout=10)

    task = asyncio.create_task(run_gateway_work(executor, block, ()))
    try:
        await asyncio.sleep(0)
        assert started.wait(timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        queued = executor.submit(lambda: "queued")
        with pytest.raises(RuntimeError, match="Gateway is busy"):
            executor.submit(lambda: "overflow")
        assert not queued.done()
    finally:
        release.set()
        executor.shutdown(wait=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("before_admission", [False, True])
async def test_cancelled_queued_turn_settles_worker_lifecycle(monkeypatch, before_admission):
    from gateway.run_executor import GatewayExecutor

    runner = _runner()
    runner._executor = executor = GatewayExecutor(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")
    monkeypatch.setattr(runner, "_adapter_for_source", lambda source: SimpleNamespace(send=AsyncMock()))

    def block():
        started.set()
        release.wait(timeout=10)

    executor.submit(block)
    ran = []
    try:
        assert started.wait(timeout=5)
        ctx = TurnContext(source=SessionSource(platform=Platform.TELEGRAM, chat_id="test-chat"))
        worker = runner._run_agent_start_turn_worker(ctx, lambda: ran.append(True))
        if not before_admission:
            await asyncio.sleep(0)
        worker.executor_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker.executor_task
        assert worker.worker_done.is_set()
    finally:
        release.set()
        executor.shutdown(wait=True)
    assert not ran


def test_shutdown_cancels_queued_proxy_and_submission_failure_releases_capacity():
    from gateway.run_executor import GatewayExecutor

    executor = GatewayExecutor(max_workers=1)
    started = threading.Event()
    release = threading.Event()

    def block():
        started.set()
        release.wait(timeout=10)

    running = executor.submit(block)
    try:
        assert started.wait(timeout=5)
        queued = executor.submit(lambda: "must not run")
        executor.shutdown(wait=False, cancel_futures=True)
        assert queued.cancelled()
        assert not running.cancel()
        with pytest.raises(RuntimeError, match="shutdown"):
            executor.submit(lambda: None)
        assert executor._outstanding == 1
    finally:
        release.set()
        executor.shutdown(wait=True)
    assert executor._outstanding == 0


@pytest.mark.asyncio
async def test_failed_queue_notice_does_not_discard_work():
    from gateway.run_executor import GatewayExecutor, run_gateway_work

    executor = GatewayExecutor(max_workers=1)
    release = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        release.wait(timeout=10)

    def fail_notice():
        release.set()
        raise OSError("simulated adapter failure")

    executor.submit(block)
    try:
        assert started.wait(timeout=5)
        result = await run_gateway_work(executor, lambda: "finished", (), AsyncMock(side_effect=fail_notice))
        assert result == "finished"
    finally:
        release.set()
        executor.shutdown(wait=True)
