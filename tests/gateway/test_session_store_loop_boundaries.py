"""Cold session routes must never acquire the guarded store lock on the loop."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import hermes_loop_guard
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def reset_loop_guard():
    hermes_loop_guard._reset_for_tests()
    yield
    hermes_loop_guard._reset_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["status", "model", "selection", "fast"])
async def test_cold_model_override_read_runs_off_loop(site, tmp_path, monkeypatch):
    import gateway.run as gateway_run
    import gateway.slash_commands_model as model_commands

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    key = runner._session_key_for_source(source)
    runner.session_store.get_or_create_session(source)
    runner.session_store.set_model_override(key, {"model": "persisted-model"})
    reads = []
    original = runner.session_store.get_model_override

    def read(session_key):
        reads.append(threading.get_ident())
        return original(session_key)  # actual LoopGuardedLock, not a pretend lock

    monkeypatch.setattr(runner.session_store, "get_model_override", read)
    monkeypatch.setattr(runner, "_normalize_source_for_session_key", lambda value: value)
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda _: "default-model")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs",
                        lambda: {"api_key": "test-key", "provider": "openai"})
    monkeypatch.setattr(runner, "_perform_model_switch", AsyncMock(return_value=(None, "stop-before-switch")))
    monkeypatch.setattr(model_commands, "_fast_route_supports", lambda *args, **kwargs: True)
    loop_thread = threading.get_ident()
    hermes_loop_guard.set_loop_thread(loop_thread)
    try:
        if site == "status":
            await runner._handle_status_command(MessageEvent(text="/status", source=source))
        elif site == "model":
            await runner._handle_model_command(MessageEvent(text="/model next-model", source=source))
        elif site == "selection":
            await runner.resolve_session_model_selection(source, "next-model", session_key=key)
        elif site == "fast":
            # The eligibility read is already off-loop on the baseline; clear its hydrated
            # snapshot before the route note so this tests the *second*, sync resolution too.
            real_apply = runner._apply_fast_selection

            def apply(*args, **kwargs):
                runner._session_model_overrides.pop(key, None)
                return real_apply(*args, **kwargs)

            monkeypatch.setattr(runner, "_apply_fast_selection", apply)
            await runner._handle_fast_command(MessageEvent(text="/fast normal", source=source))
        assert reads and loop_thread not in reads, f"{site} store read on loop: {reads}"
        if site != "fast":
            assert runner._session_model_override(key)["model"] == "persisted-model"
    finally:
        hermes_loop_guard.set_loop_thread(None)
        if runner._executor is not None:
            await asyncio.to_thread(runner._executor.shutdown)


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["topic", "heartbeat", "heartbeat-restore"])
async def test_session_owner_peek_runs_off_loop(site, monkeypatch):
    from gateway.telegram_topic_sessions import _topic_exists
    from gateway.run_heartbeat_restore import restore_heartbeat_watches

    reads = []
    lock = hermes_loop_guard.LoopGuardedLock("session-store lock")

    def peek(key):
        reads.append(threading.get_ident())
        with lock:
            return "sid"

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(peek_session_id=peek)
    runner._session_db = None
    runner._delivery_adapter_for = lambda _: None
    runner._warm_goals_session_db = AsyncMock()
    runner._run_in_executor_with_context = AsyncMock(return_value=[("k", source, "sid")])
    runner._register_heartbeat_watch = lambda *args: None
    loop_thread = threading.get_ident()
    hermes_loop_guard.set_loop_thread(loop_thread)
    if site == "topic":
        assert await _topic_exists(runner, source, "42")
    elif site == "heartbeat":
        await runner._heartbeat_poll_watch({}, "k", source, "sid")
    else:
        await restore_heartbeat_watches(runner)
    assert reads and loop_thread not in reads


@pytest.mark.asyncio
async def test_async_hydration_rejects_stale_read_after_edit(tmp_path, monkeypatch):
    """Off-loop reads must retain the synchronous hydration's epoch/edit-pending fence."""
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def load(key):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return {"model": "stale-model"}

    monkeypatch.setattr(runner, "_load_session_model_override", load)
    hydrate = asyncio.create_task(runner._rehydrate_session_model_override_async("k"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runner._bump_session_model_override_epoch("k")
        runner._session_model_overrides["k"] = {"model": "new-model"}
        release.set()
        await asyncio.wait_for(hydrate, 5)
        assert runner._session_model_override("k")["model"] == "new-model"
    finally:
        release.set()
        await asyncio.gather(hydrate, return_exceptions=True)
        if runner._executor is not None:
            await asyncio.to_thread(runner._executor.shutdown)



@pytest.mark.parametrize("outcome,code", [("success", 0), ("failed", 1), ("restart", 75)])
def test_gateway_process_exits_before_asyncio_executor_join(outcome, code, tmp_path):
    """A default-executor job and non-daemon worker must not outlive teardown."""
    script = textwrap.dedent('''
        import asyncio, sys, threading
        import gateway.run as run
        outcome = sys.argv[1]
        block, started = threading.Event(), threading.Event()
        def stuck():
            started.set()
            block.wait()
        async def fake_start(_config=None):
            asyncio.get_running_loop().run_in_executor(None, stuck)
            while not started.is_set():
                await asyncio.sleep(0)
            threading.Thread(target=block.wait, daemon=False).start()
            print("Gateway stopped", flush=True)
            if outcome == "restart":
                raise SystemExit(75)
            return outcome == "success"
        run.start_gateway = fake_start
        sys.argv = ["gateway.run"]
        run.main()
    ''')
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "home"),
               HERMES_RUNTIME_DIR=str(tmp_path / "runtime"))
    child = subprocess.Popen([sys.executable, "-c", script, outcome], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env,
                             cwd=Path(__file__).resolve().parents[2])
    try:
        try:
            stdout, stderr = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            stdout, stderr = child.communicate(timeout=5)
            pytest.fail(f"Gateway stopped but process remained alive: {stdout}\n{stderr}")
        assert "Gateway stopped" in stdout, stderr
        assert child.returncode == code, (stdout, stderr)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
