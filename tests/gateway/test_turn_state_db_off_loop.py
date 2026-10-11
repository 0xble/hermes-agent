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
    original_write = db.set_meta
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

            monkeypatch.setattr(db, "set_meta", write)
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
