"""Hydration cannot resurrect a pending durable clear or overwrite a picker."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.mark.asyncio
async def test_model_hydration_waits_for_pending_resume_clear(monkeypatch, tmp_path):
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    key = runner._session_key_for_source(source)
    runner.session_store.get_or_create_session(source)
    runner.session_store.set_model_override(key, {"model": "stale-model"})
    runner._session_model_overrides[key] = {"model": "stale-model"}
    runner._session_db = SimpleNamespace(get_session_title=AsyncMock(return_value="target"))
    monkeypatch.setattr(runner, "_resolve_resume_target", AsyncMock(return_value=("target", "target")))
    monkeypatch.setattr(runner, "_resume_access_denied_reply", AsyncMock(return_value=None))
    monkeypatch.setattr(runner, "_normalize_source_for_session_key", lambda value: value)
    monkeypatch.setattr(runner.session_store, "load_transcript", lambda _: [])
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda _: "default-model")
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs",
                        lambda: {"api_key": "default-key", "provider": "probe"})
    loop = asyncio.get_running_loop()
    write_started = asyncio.Event()
    release_write = threading.Event()
    hydration_done = threading.Event()
    original_write = runner.session_store.set_model_override

    def pending_write(session_key, override):
        assert override is None
        loop.call_soon_threadsafe(write_started.set)
        assert release_write.wait(10), "clear was not released"
        original_write(session_key, override)

    monkeypatch.setattr(runner.session_store, "set_model_override", pending_write)
    resume = asyncio.create_task(runner._handle_resume_command(MessageEvent(text="/resume target", source=source)))
    hydration = None
    controller = None
    try:
        await asyncio.wait_for(write_started.wait(), 10)
        assert runner._session_model_override(key) is None

        async def hydrate():
            try:
                return await runner._resolve_session_agent_runtime_async(source=source, session_key=key)
            finally:
                hydration_done.set()

        # On the broken head hydration completes against the OLD durable value.
        # Serialization makes it wait; the independent controller's 2s bound
        # releases the writer without relying on the event loop to time it out.
        def release_after_hydration_attempt():
            hydration_done.wait(2)
            release_write.set()

        hydration = asyncio.create_task(hydrate())
        controller = threading.Thread(target=release_after_hydration_attempt, daemon=True)
        controller.start()
        await asyncio.wait_for(asyncio.gather(resume, hydration), 10)
        persisted = runner.session_store.get_model_override(key)
        live = runner._session_model_override(key)
        assert live is None and persisted is None, (
            f"pending clear resurrected override: persisted={persisted!r}, live={live!r}"
        )
        assert hydration.result()[0] == "default-model"
    finally:
        release_write.set()
        await asyncio.gather(*(task for task in (resume, hydration) if task is not None), return_exceptions=True)
        if controller is not None:
            await asyncio.to_thread(controller.join, 10)
        if runner._executor is not None:
            await asyncio.to_thread(runner._executor.shutdown)


@pytest.mark.asyncio
async def test_sync_hydration_during_pending_clear_cannot_resurrect_override(monkeypatch, tmp_path):
    """Synchronous resolvers (TurnRunner.run_sync, /fast's route note) cannot await the route
    lock. While a durable clear is pending they must not hydrate the old stored value."""
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    key = runner._session_key_for_source(source)
    runner.session_store.get_or_create_session(source)
    runner.session_store.set_model_override(key, {"model": "stale-model"})
    runner._session_model_overrides[key] = {"model": "stale-model"}
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda _: "default-model")
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs",
                        lambda: {"api_key": "default-key", "provider": "probe"})
    loop = asyncio.get_running_loop()
    write_started = asyncio.Event()
    release_write = threading.Event()
    original_write = runner.session_store.set_model_override

    def pending_write(session_key, override):
        loop.call_soon_threadsafe(write_started.set)
        assert release_write.wait(10), "clear was not released"
        original_write(session_key, override)

    monkeypatch.setattr(runner.session_store, "set_model_override", pending_write)
    runner._session_model_overrides.pop(key, None)  # the clear's in-memory half, as /resume does first
    clear = asyncio.create_task(runner._persist_session_model_override(key, None))
    try:
        await asyncio.wait_for(write_started.wait(), 10)
        model, _runtime = runner._resolve_session_agent_runtime(session_key=key)
        assert model == "default-model", f"sync hydration resurrected {model!r} during a pending clear"
        release_write.set()
        await asyncio.wait_for(clear, 10)
        live = runner._session_model_override(key)
        persisted = runner.session_store.get_model_override(key)
        assert live is None and persisted is None, (
            f"pending clear resurrected override: persisted={persisted!r}, live={live!r}"
        )
    finally:
        release_write.set()
        await asyncio.gather(clear, return_exceptions=True)
        if runner._executor is not None:
            await asyncio.to_thread(runner._executor.shutdown)


@pytest.mark.asyncio
async def test_reasoning_hydration_cannot_overwrite_newer_picker(monkeypatch, tmp_path):
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    key = runner._session_key_for_source(source)
    runner.session_store.get_or_create_session(source)
    runner.session_store.set_reasoning_override(key, {"enabled": True, "effort": "low"})
    monkeypatch.setattr(runner, "_normalize_source_for_session_key", lambda value: value)
    monkeypatch.setattr(runner, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(runner, "_try_send_choice_picker", AsyncMock(return_value=True))
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda **_: {})
    loop = asyncio.get_running_loop()
    read_started = threading.Event()
    release_read = threading.Event()
    picker_applied = threading.Event()
    read = runner.session_store.get_reasoning_override
    owner_thread = threading.get_ident()
    read_threads = []
    controller_errors = []

    def stale_read(session_key):
        snapshot = read(session_key)
        read_threads.append(threading.get_ident())
        read_started.set()
        assert release_read.wait(10), "reasoning read was not released"
        return snapshot

    def pick_high():
        try:
            runner._apply_reasoning_selection(key, "telegram", "high")
        finally:
            picker_applied.set()

    def schedule_picker_during_read():
        try:
            assert read_started.wait(10), "reasoning read did not start"
            loop.call_soon_threadsafe(pick_high)
            # With the resolver on-loop the picker cannot interleave with its
            # snapshot/commit. Off-loop it applies high before stale low commits.
            picker_applied.wait(2)
        except BaseException as exc:
            controller_errors.append(exc)
        finally:
            release_read.set()

    monkeypatch.setattr(runner.session_store, "get_reasoning_override", stale_read)
    controller = threading.Thread(target=schedule_picker_during_read, daemon=True)
    controller.start()
    try:
        await runner._handle_reasoning_command(MessageEvent(text="/reasoning", source=source))
        await asyncio.wait_for(asyncio.to_thread(picker_applied.wait, 10), 12)
        persisted = read(key)
        live = runner._session_reasoning_overrides[key]
        assert live == persisted == {"enabled": True, "effort": "high"}, (
            f"reasoning hydration overwrote picker: persisted={persisted!r}, live={live!r}"
        )
        assert read_threads == [owner_thread], "reasoning resolver mutated session state off-loop"
        assert not controller_errors
    finally:
        release_read.set()
        await asyncio.to_thread(controller.join, 10)
        if runner._executor is not None:
            await asyncio.to_thread(runner._executor.shutdown)


@pytest.mark.asyncio
@pytest.mark.parametrize("store_mode", ["absent", "none"])
@pytest.mark.parametrize("live_role", [None, "coding"])
async def test_model_selection_hydration_without_store_preserves_live_state(store_mode, live_role):
    runner = object.__new__(GatewayRunner)
    if store_mode == "none":
        runner.session_store = None
    key = "agent:main:telegram:dm:123"
    state = runner._session_state(key)
    state.conversation.model_role = live_role
    override = {"model": "live-pick"} if live_role is None else None
    state.conversation.model_override = override
    state.conversation.model_override_scope = "once"
    state.persistent.model_override_epoch = 7

    await runner._rehydrate_session_model_selection_async(key)

    assert state.conversation.model_role == live_role
    assert state.conversation.model_override is override
    assert state.conversation.model_override_scope == "once"
    assert state.persistent.model_override_epoch == 7
