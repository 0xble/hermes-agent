"""A contended state.db must not stop per-turn/notification loop dispatch."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_state import SessionDB


async def _assert_loop_live(operation, install_blocker):
    """Release the storage stall only after a heartbeat, with a bounded RED escape.

    The controller is independent of asyncio, so a broken implementation cannot
    deadlock the test's own timeout. No scheduling sleeps or latency measurements.
    """
    loop = asyncio.get_running_loop()
    entered = threading.Event()
    heartbeat = threading.Event()
    release = threading.Event()
    started = asyncio.Event()
    serviced = []

    def block():
        entered.set()
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10), "storage controller did not release"

    install_blocker(block)

    def controller():
        try:
            if entered.wait(10):
                serviced.append(heartbeat.wait(2))
        finally:
            release.set()

    async def pulse():
        await started.wait()
        heartbeat.set()

    control = threading.Thread(target=controller, daemon=True)
    control.start()
    ticker = asyncio.create_task(pulse())
    try:
        result = await operation()
    finally:
        release.set()
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        await asyncio.to_thread(control.join, 10)
    assert serviced == [True], "state.db contention blocked the event-loop heartbeat"
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "goal",
        "revive",
        "revive-write",
        "event-manager",
        "loop",
        "notification",
        "model",
        "anchor",
        "cooldown",
    ],
)
async def test_turn_storage_contention_keeps_loop_live(path, monkeypatch, tmp_path):
    from hermes_cli import goals

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="123", user_id="u1", chat_type="dm"
    )
    event = MessageEvent(text="continue", source=source)
    entry = SimpleNamespace(
        session_id="session-a", last_prompt_tokens=0 if path == "anchor" else 100
    )
    db = SessionDB(tmp_path / "probe.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    monkeypatch.setattr(
        runner, "_session_entry_for_manager", AsyncMock(return_value=entry)
    )
    monkeypatch.setattr(runner, "_send_goal_status_notice", AsyncMock())
    if path in {"anchor", "cooldown"}:
        runner._session_db = SimpleNamespace(_db=db)
        monkeypatch.setattr(
            "agent.model_metadata.get_model_context_length_async",
            AsyncMock(return_value=100),
        )
    original_read = db.get_meta
    # Goal mutations commit through SessionDB's write transaction (re-read + write under BEGIN
    # IMMEDIATE), not set_meta: block that real boundary so the proof still covers the write.
    original_write = db._execute_write
    if path == "revive-write":
        goals.save_goal(
            "session-a",
            goals.GoalState(
                goal="probe",
                status="paused",
                last_verdict="blocked",
                paused_reason=goals._BLOCKED_PAUSE_PREFIX + "needs input",
            ),
        )

    def install(block):
        def read(key, *args, **kwargs):
            block()
            return original_read(key, *args, **kwargs)

        if path == "revive-write":

            def write(*args, **kwargs):
                block()
                return original_write(*args, **kwargs)

            monkeypatch.setattr(db, "_execute_write", write)
        else:
            monkeypatch.setattr(db, "get_meta", read)
        if path == "notification":
            monkeypatch.setattr(runner.session_store, "_ensure_loaded", block)
        elif path == "anchor":

            def anchor(*args, **kwargs):
                block()
                return 1

            monkeypatch.setattr("agent.usage_anchor.persisted_anchor_tokens", anchor)
        elif path == "cooldown":

            def cooldown(*args, **kwargs):
                block()
                return {"remaining_seconds": 5}

            monkeypatch.setattr(db, "get_compression_failure_cooldown", cooldown)
        elif path == "model":

            def override(key):
                block()
                return {"model": "chosen-model", "provider": "probe"}

            monkeypatch.setattr(runner.session_store, "get_model_override", override)
            monkeypatch.setattr(
                "gateway.run._resolve_runtime_agent_kwargs_for_provider",
                lambda *args, **kwargs: {"api_key": "test-key", "provider": "probe"},
            )
            monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})

    async def operation():
        if path == "goal":
            return await runner._post_turn_goal_continuation(
                session_entry=entry, source=source, final_response="finished"
            )
        if path in {"revive", "revive-write"}:
            return await runner._revive_blocked_goal_for_user_turn(entry, source, event)
        if path == "event-manager":
            return await runner._get_goal_manager_for_event(event)
        if path == "loop":
            return await runner._post_turn_loop_completion(
                session_entry=entry, source=source, final_response="finished"
            )
        if path == "notification":
            async with runner._completion_event_scope({
                "session_key": "agent:main:telegram:dm:123"
            }):
                return "scoped"
        if path in {"anchor", "cooldown"}:
            hs = SimpleNamespace(
                model="probe",
                base_url="",
                api_key="",
                provider="",
                api_mode="",
                config_context_length=100,
                threshold_pct=0.8,
                hard_msg_limit=5000,
            )
            return await runner._hmwa_hygiene_plan(
                hs, [{"role": "user", "content": "hi"}], entry, "key"
            )
        return await runner._hmwa_hygiene_settings(source, "agent:main:telegram:dm:123")

    try:
        result = await _assert_loop_live(operation, install)
        if path == "revive-write":
            assert goals.load_goal("session-a").status == "active"
            runner._send_goal_status_notice.assert_awaited_once()
        if path in {"anchor", "cooldown"}:
            assert result.needs_compress is False
        if path == "model":
            assert result.model == "chosen-model"
            assert result.api_key == "test-key"
    finally:
        db.close()
        executor = getattr(runner, "_executor", None)
        if executor is not None:
            await asyncio.to_thread(executor.shutdown)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "command"])
async def test_telegram_recovers_before_batching_without_blocking_loop(
    kind, monkeypatch
):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="123", user_id="u1", chat_type="dm"
    )
    event = MessageEvent(
        text="x" * adapter._SPLIT_THRESHOLD,
        source=source,
        message_type=MessageType.COMMAND if kind == "command" else MessageType.TEXT,
    )
    msg = SimpleNamespace(text=event.text)
    update = SimpleNamespace(message=msg)
    monkeypatch.setattr(adapter, "_effective_update_message", lambda _: msg)
    monkeypatch.setattr(adapter, "_is_user_authorized_from_message", lambda _: True)
    monkeypatch.setattr(adapter, "_gate_or_observe", lambda *args: True)
    monkeypatch.setattr(
        adapter, "_should_process_message", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(adapter, "_ensure_forum_commands", AsyncMock())
    monkeypatch.setattr(
        adapter, "_build_triggered_event", AsyncMock(return_value=event)
    )
    monkeypatch.setattr(adapter, "_should_drop_delayed_delivery", lambda: False)
    accepted = []
    monkeypatch.setattr(
        adapter, "_accept_update", lambda: accepted.append(event.source.thread_id)
    )

    def install(block):
        def recover(source):
            block()
            return "77"

        adapter.set_topic_recovery_fn(recover)

    async def operation():
        handler = (
            adapter._handle_command
            if kind == "command"
            else adapter._handle_text_message
        )
        await handler(update, None)

    try:
        await _assert_loop_live(operation, install)
        assert accepted == ["77"]
        assert len(adapter._pending_text_batches) == 1
        assert next(iter(adapter._pending_text_batches)) == adapter._text_batch_key(
            event
        )
        assert event.source.thread_id == "77"
    finally:
        tasks = list(adapter._pending_text_batch_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class _BlockedRead:
    """Freeze a store result until the loop has performed the competing operation."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = threading.Event()
        self.loop = asyncio.get_running_loop()

    def block(self):
        self.loop.call_soon_threadsafe(self.started.set)
        assert self.release.wait(10), "competing operation did not release the read"


@pytest.mark.asyncio
async def test_stop_interrupts_original_before_blocked_marker_read(monkeypatch, tmp_path):
    from gateway.session import build_session_key
    from unittest.mock import MagicMock

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    key = build_session_key(source)
    runner.session_store.get_or_create_session(source)
    runner.session_store.mark_resume_pending(key, "original-recovery")
    original, successor = MagicMock(), MagicMock()
    original.session_id = "original-turn"
    successor.session_id = "successor-turn"
    original_generation = runner._begin_session_run_generation(key)
    runner._running_agents[key] = original
    runner._agent_cache[key] = (original, "original-signature")
    monkeypatch.setattr(runner, "_persist_active_agents", lambda: None)
    monkeypatch.setattr(runner, "_pause_goal_for_stop", AsyncMock())
    adapter = SimpleNamespace(_invalidate_deferred_commands=MagicMock())
    monkeypatch.setattr(runner, "_delivery_adapter_for", lambda _: adapter)
    gate = _BlockedRead()
    read = runner.session_store.get_resume_pending_marker

    def blocked_read(session_key):
        gate.block()
        # Deliberately read AFTER replacement: the generation fence must also
        # protect a recovery whose immutable marker was not available at /stop.
        return read(session_key)

    monkeypatch.setattr(runner.session_store, "get_resume_pending_marker", blocked_read)
    stop = asyncio.create_task(runner._interrupt_and_clear_session(
        key, source, interrupt_reason="Stop requested", invalidation_reason="stop_command"
    ))
    try:
        await asyncio.wait_for(gate.started.wait(), 10)
        interrupted_before_read = original.interrupt.call_count
        generation_before_read = runner._current_session_run_generation(key)
        successor_generation = runner._begin_session_run_generation(key)
        runner._running_agents[key] = successor
        runner._agent_cache[key] = (successor, "successor-signature")
        runner._pending_messages[key] = "successor-command"
        runner.session_store.mark_resume_pending(key, "successor-recovery")
        successor_marker = read(key)
        gate.release.set()
        await asyncio.wait_for(stop, 10)
        assert interrupted_before_read == 1, "original turn was not interrupted before storage await"
        assert generation_before_read == original_generation + 1
        successor.interrupt.assert_not_called()
        assert runner._current_session_run_generation(key) == successor_generation
        assert runner._running_agents[key] is successor
        assert runner._agent_cache[key][0] is successor
        assert runner._pending_messages[key] == "successor-command"
        adapter._invalidate_deferred_commands.assert_not_called()
        runner._pause_goal_for_stop.assert_not_awaited()
        assert successor_marker is not None and read(key) == successor_marker
    finally:
        gate.release.set()
        await asyncio.gather(stop, return_exceptions=True)
        if runner._executor is not None:
            await asyncio.to_thread(runner._executor.shutdown)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["model", "boundary"])
@pytest.mark.parametrize("phase", ["store", "runtime"])
async def test_rehydrate_does_not_overwrite_concurrent_choice(change, phase, monkeypatch, tmp_path):
    from gateway.session import build_session_key
    from gateway.slash_commands_model import _ModelSwitchContext

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    key = build_session_key(source)
    runner.session_store.get_or_create_session(source)
    runner.session_store.set_model_override(key, {"model": "old-model", "provider": "probe"})
    runner._session_db = None
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda _: "default-model")
    gate = _BlockedRead()
    credential_calls = 0

    def resolve_credentials(*args, **kwargs):
        nonlocal credential_calls
        credential_calls += 1
        if phase == "runtime":
            if credential_calls == 1:
                return {"provider": "probe"}  # hydration is credential-less; resolve again
            if credential_calls == 2:
                gate.block()
        return {"api_key": "test-key", "provider": "probe"}

    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", resolve_credentials)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs",
                        lambda: {"api_key": "default-key", "provider": "probe"})
    read = runner.session_store.get_model_override

    def blocked_read(session_key):
        snapshot = read(session_key)
        if phase == "store":
            gate.block()
        return snapshot

    monkeypatch.setattr(runner.session_store, "get_model_override", blocked_read)
    owner_thread = threading.get_ident()
    state_access_threads = []
    session_state = runner._session_state

    def observed_state(session_key):
        state_access_threads.append(threading.get_ident())
        return session_state(session_key)

    monkeypatch.setattr(runner, "_session_state", observed_state)
    loop = asyncio.get_running_loop()
    runtime_started = asyncio.Event()
    runtime_release = threading.Event()
    if phase == "store":
        load_runtime = runner._load_session_agent_runtime

        def blocked_runtime(**kwargs):
            loop.call_soon_threadsafe(runtime_started.set)
            assert runtime_release.wait(10), "runtime resolution did not release"
            return load_runtime(**kwargs)

        # Keep the detached runtime snapshot behind an event while the queued switch commits. Without
        # this barrier, awaiting the switch task does not order it before an already-finished worker
        # snapshot, so the assertion observes a valid old-model resolution and flakes by task scheduling.
        monkeypatch.setattr(runner, "_load_session_agent_runtime", blocked_runtime)
    else:
        runtime_release.set()
    resolution = asyncio.create_task(runner._resolve_session_agent_runtime_async(
        source=source, session_key=key
    ))
    try:
        await asyncio.wait_for(gate.started.wait(), 10)
        if change == "model":
            result = SimpleNamespace(
                new_model="new-model", target_provider="probe", api_key="new-key",
                base_url="https://probe.invalid/v1", api_mode="chat_completions",
                request_overrides={}, runtime_capabilities={}, provider_label="probe",
            )
            ctx = _ModelSwitchContext(key, source, tmp_path / "config.yaml", False,
                                      current_model="old-model")
            switch_started = asyncio.Event()

            async def switch_model():
                switch_started.set()
                await runner._record_model_switch(result, ctx, source=source, one_turn=False, picker=False)

            switch = asyncio.create_task(switch_model())
            await asyncio.wait_for(switch_started.wait(), 5)
            # The switch queues behind hydration. Release the read, then hold the detached runtime
            # snapshot until the lock-owning switch has committed.
            gate.release.set()
            if phase == "store":
                await asyncio.wait_for(runtime_started.wait(), 5)
            await asyncio.wait_for(switch, 5)
            assert runner._session_model_override(key)["model"] == "new-model"
        else:
            runner._clear_conversation_scope(key, reason="session_reset")
            await runner.async_session_store.set_model_override(key, None)
            assert runner._session_model_override(key) is None
            gate.release.set()
            if phase == "store":
                await asyncio.wait_for(runtime_started.wait(), 5)
        runtime_release.set()
        model, _ = await asyncio.wait_for(resolution, 10)
        override = runner._session_model_override(key)
        assert (override or {}).get("model") == ("new-model" if change == "model" else None), (
            "stale rehydrate replaced the concurrent model choice or conversation boundary"
        )
        assert model == ("new-model" if change == "model" else "default-model")
        assert state_access_threads and set(state_access_threads) == {owner_thread}
        assert (read(key) or {}).get("model") == ("new-model" if change == "model" else None)
    finally:
        gate.release.set()
        runtime_release.set()
        await asyncio.gather(resolution, return_exceptions=True)
        if runner._executor is not None:
            await asyncio.to_thread(runner._executor.shutdown)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/subgoal cite the failing test", "/heartbeat every 10m check CI",
                                     "/loop 5m poll CI"])
async def test_goal_family_commands_keep_loop_live(command, monkeypatch, tmp_path):
    """Manager writes behind /subgoal, /heartbeat and /loop take the writer lock: off the loop."""
    from hermes_cli import goals

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1", chat_type="dm")
    event = MessageEvent(text=command, message_type=MessageType.COMMAND, source=source)
    entry = SimpleNamespace(session_id="session-a", session_key="agent:main:telegram:dm:123")
    db = SessionDB(tmp_path / "probe.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    monkeypatch.setattr(runner, "_session_entry_for_manager", AsyncMock(return_value=entry))
    monkeypatch.setattr(runner.async_session_store, "get_or_create_session", AsyncMock(return_value=entry))
    monkeypatch.setattr(runner, "_register_heartbeat_watch", lambda *args: None)
    goals.save_goal("session-a", goals.GoalState(goal="probe", status="active"))
    # Goal mutations commit through SessionDB's write transaction, not set_meta; heartbeat and
    # loop writes reach the same boundary via set_meta -> _write_sql -> _execute_write.
    original_write = db._execute_write

    def install(block):
        # Block the command's own write: manager construction is already off the loop.
        def write(*args, **kwargs):
            block()
            return original_write(*args, **kwargs)

        monkeypatch.setattr(db, "_execute_write", write)

    handler = {"/subgoal": runner._handle_subgoal_command, "/heartbeat": runner._handle_heartbeat_command,
               "/loop": runner._handle_loop_command}[command.split()[0]]
    try:
        reply = await _assert_loop_live(lambda: handler(event), install)
        assert reply
    finally:
        db.close()
        executor = getattr(runner, "_executor", None)
        if executor is not None:
            await asyncio.to_thread(executor.shutdown)
