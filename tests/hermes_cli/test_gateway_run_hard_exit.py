"""Regression tests for CLI gateway run exit behavior.

``hermes gateway run`` enters through hermes_cli.gateway, not gateway.run.main().
After graceful teardown it must use the same hard-exit backstop as gateway.run.main()
so Python finalization does not wait on non-daemon worker threads (for example
in-flight cron ThreadPoolExecutor jobs) and delay service-managed restarts.
"""

from __future__ import annotations

import asyncio
import types

import pytest


class _HardExitObserved(BaseException):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def _prepare(monkeypatch):
    import hermes_cli.gateway as gateway_cli
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_cli, "_guard_official_docker_root_gateway", lambda: None)
    monkeypatch.setattr(gateway_cli, "_guard_named_profile_under_multiplexer", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_attach_to_host_gateway_or_guard", lambda **kwargs: None)
    monkeypatch.setattr(gateway_cli, "_guard_supervised_gateway_conflict", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_guard_existing_gateway_process_conflict", lambda replace=False: None)
    monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway_cli.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setenv("HERMES_GATEWAY_EXIT_DIAG", "0")

    async def _start_gateway(*args, **kwargs):  # pragma: no cover - never awaited by fake run
        return True

    def _hard_exit(code: int) -> None:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            pass
        else:
            loop.close()
            asyncio.set_event_loop(None)
        raise _HardExitObserved(code)

    monkeypatch.setattr(gateway_run, "start_gateway", _start_gateway)
    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", _hard_exit)
    return gateway_cli


def test_gateway_loop_returns_without_joining_default_executor():
    import threading
    import time
    import hermes_cli.gateway as gateway_cli

    release = threading.Event()
    started = threading.Event()

    def stuck():
        started.set()
        release.wait(5)

    async def teardown():
        asyncio.get_running_loop().run_in_executor(None, stuck)
        while not started.is_set():
            await asyncio.sleep(0)
        return True

    began = time.monotonic()
    assert gateway_cli._run_gateway_until_teardown(teardown()) is True
    assert time.monotonic() - began < 2.0
    release.set()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(loop.shutdown_default_executor())
    loop.close()
    asyncio.set_event_loop(None)


def test_gateway_loop_finishes_cancelled_writes_and_async_generators(tmp_path):
    """Teardown must run finally blocks before reaching the hard-exit funnel."""
    import hermes_cli.gateway as gateway_cli

    finally_path = tmp_path / "finally.txt"
    generator_path = tmp_path / "generator.txt"
    retained = []

    async def pending():
        try:
            await asyncio.Future()
        finally:
            await asyncio.shield(asyncio.to_thread(finally_path.write_text, "written"))

    async def generator():
        try:
            yield 1
        finally:
            await asyncio.to_thread(generator_path.write_text, "closed")

    async def teardown():
        task = asyncio.create_task(pending())
        retained.append(task)
        gen = generator()
        retained.append(gen)
        assert await anext(gen) == 1
        await asyncio.sleep(0)
        return True

    try:
        assert gateway_cli._run_gateway_until_teardown(teardown()) is True
        assert finally_path.read_text() == "written"
        assert generator_path.read_text() == "closed"
        assert retained[0].cancelled()
    finally:
        loop = asyncio.get_event_loop()
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
        asyncio.set_event_loop(None)


def test_gateway_loop_preserves_inflight_shielded_model_write(tmp_path):
    import threading
    import time
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from gateway.event_loop import run_until_teardown

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    key = runner._session_key_for_source(source)
    runner.session_store.get_or_create_session(source)
    original_write = runner.session_store.set_model_override
    started = threading.Event()
    edits = []

    def write(*args):
        started.set()
        time.sleep(0.05)
        return original_write(*args)

    runner.session_store.set_model_override = write

    async def teardown():
        edits.append(asyncio.create_task(runner._persist_session_model_override(key, {"model": "durable"})))
        while not started.is_set():
            await asyncio.sleep(0)
        return True

    try:
        assert run_until_teardown(teardown()) is True
        assert edits[0].cancelled()
        assert runner.session_store.get_model_override(key)["model"] == "durable"
        assert runner._session_model_override(key)["model"] == "durable"
    finally:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True))
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
        asyncio.set_event_loop(None)
        if runner._session_db is not None:
            runner._session_db.close()


def test_gateway_loop_bounds_uncooperative_task_and_asyncgen_cleanup(monkeypatch):
    import time
    import gateway.event_loop as gateway_loop

    monkeypatch.setattr(gateway_loop, "_TASK_CLEANUP_TIMEOUT", 0.03)
    monkeypatch.setattr(gateway_loop, "_ASYNCGEN_CLEANUP_TIMEOUT", 0.03)
    release = asyncio.Event()
    retained = []

    async def ignore_cancellation():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    async def generator():
        try:
            yield 1
        finally:
            await ignore_cancellation()

    async def teardown():
        retained.append(asyncio.create_task(ignore_cancellation()))
        gen = generator()
        retained.append(gen)
        await anext(gen)
        await asyncio.sleep(0)
        return True

    try:
        began = time.monotonic()
        assert gateway_loop.run_until_teardown(teardown()) is True
        assert time.monotonic() - began < 0.5
        assert not retained[0].done()
    finally:
        loop = asyncio.get_event_loop()
        release.set()
        loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


def test_run_gateway_hard_exits_after_clean_return(monkeypatch):
    gateway_cli = _prepare(monkeypatch)

    with pytest.raises(_HardExitObserved) as excinfo:
        gateway_cli.run_gateway()

    assert excinfo.value.code == 0


def test_run_gateway_hard_exits_after_keyboard_interrupt(monkeypatch):
    """KeyboardInterrupt (console Ctrl+C) must also hard-exit, not return.

    A bare ``return`` would let Python finalization join non-daemon worker
    threads, the same wedge this backstop prevents on the other exit paths.
    """
    gateway_cli = _prepare(monkeypatch)

    def _fake_run(coro):
        coro.close()
        raise KeyboardInterrupt()

    monkeypatch.setattr(gateway_cli, "_run_gateway_until_teardown", _fake_run)

    with pytest.raises(_HardExitObserved) as excinfo:
        gateway_cli.run_gateway()

    assert excinfo.value.code == 0
