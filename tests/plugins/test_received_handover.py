"""Received Telegram work survives handover through the real adapter/runner gates."""

import asyncio
import threading
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import User
from telegram.ext import Application, TypeHandler

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.zero_cut_handover_drain import request_handover, status
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.restart_test_helpers import make_restart_runner


async def _ready(event):
    await asyncio.wait_for(event.wait(), 5)


async def _enqueue(adapter, kind, event):
    if kind == "text":
        adapter._enqueue_text_event(event)
    elif kind == "photo":
        adapter._enqueue_photo_event("photos", event)
    else:
        await adapter._queue_media_group_event("album", event)


def _runner(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr("gateway.zero_cut_handover_drain.is_zero_cut_handover_enabled", lambda config: True)
    monkeypatch.setattr("cron.scheduler_review.stop_reviews", lambda: None)
    runner, _ = make_restart_runner()
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._running = True
    runner._mark_attended_user_activity = AsyncMock(return_value=False)
    runner._clear_durable_active_turn = AsyncMock()
    runner._run_post_turn_hooks = AsyncMock()
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda ev, source: ev)
    runner._is_user_authorized_for_source = lambda source: True
    runner._admit_bot_message_for_source = lambda source: True
    runner._persist_active_agents = lambda: None
    runner._session_control_mutation_pending = lambda *args: False
    runner._retain_background_task = lambda task: setattr(runner, "handover_task", task)
    runner.stop = AsyncMock()
    return runner


def _adapter(runner, monkeypatch, *, secondary=False):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="123:test"))
    adapter.gateway_runner = runner
    adapter.set_message_handler(runner._handle_message)
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="reply"))
    adapter.send_typing = AsyncMock()
    adapter.on_processing_complete = AsyncMock()
    monkeypatch.setattr(adapter, "_text_batch_delay_for", lambda event: 0)
    adapter._media_batch_delay_seconds = 0
    monkeypatch.setattr(adapter, "MEDIA_GROUP_WAIT_SECONDS", 0)
    if secondary:
        runner._profile_adapters = {"secondary": {Platform.TELEGRAM: adapter}}
    else:
        runner.adapters = {Platform.TELEGRAM: adapter}
    return adapter


def _event(adapter, kind, *, secondary=False):
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="123", user_id="456", chat_type="dm",
        profile="secondary" if secondary else None,
    )
    source._transport_adapter_ref = weakref.ref(adapter)
    return MessageEvent(
        text="Preserve my original received ask.", message_id="8123", source=source,
        message_type=MessageType.TEXT if kind == "text" else MessageType.PHOTO,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "photo", "album"])
@pytest.mark.parametrize("secondary", [False, True], ids=["primary", "secondary"])
@pytest.mark.parametrize("phase", ["settling", "released", "cancelled"])
async def test_received_batches_execute_once_across_handover(kind, secondary, phase, monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    adapter = _adapter(runner, monkeypatch, secondary=secondary)
    event = _event(adapter, kind, secondary=secondary)
    fetched, dispatch = asyncio.Event(), asyncio.Event()
    popped, before_admission, admit = asyncio.Event(), asyncio.Event(), asyncio.Event()
    released, executed, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    recover = threading.Event()
    calls, responses = [], []
    handled, transport_settle = asyncio.Event(), asyncio.Event()
    release_observations = []

    async def runner_dispatch(received):
        result = await runner._handle_message(received)
        responses.append(result)
        handled.set()
        return result

    adapter.set_message_handler(runner_dispatch)
    loop = asyncio.get_running_loop()

    # Real PTB processor and update queue, no Telegram network or token ownership.
    app = Application.builder().token("123:test").concurrent_updates(4).build()
    monkeypatch.setattr(type(app.bot), "initialize", AsyncMock())
    monkeypatch.setattr(type(app.bot), "shutdown", AsyncMock())
    app.bot._bot_user = User(123, "test", is_bot=True)
    adapter._app = app

    async def fetched_update(update, context):
        fetched.set()
        await dispatch.wait()
        await _enqueue(adapter, kind, update)
        await transport_settle.wait()

    app.add_handler(TypeHandler(MessageEvent, fetched_update))

    def recover_topic(received):
        loop.call_soon_threadsafe(popped.set)
        assert recover.wait(5), "test did not release topic recovery"

    adapter._apply_topic_recovery = recover_topic
    adapter._topic_recovery_fn = lambda source: None

    async def processing_hook(name, received, *args):
        if name == "on_processing_start":
            before_admission.set()
            await admit.wait()

    adapter._run_processing_hook = processing_hook

    async def execute(received, source, key, generation):
        calls.append((received.message_id, received.text, source.profile))
        executed.set()
        await finish.wait()
        return "Executed original ask."

    runner._handle_message_with_agent = execute

    def release_lock():
        # Token ownership cannot transfer while a batch is popped but not handed off.
        release_observations.append((before_admission.is_set(), runner._external_drain_active))
        released.set()

    adapter._release_platform_lock = release_lock
    stopped = asyncio.Event()
    def stop_polling():
        app.updater._running = False
        stopped.set()

    monkeypatch.setattr(type(app.updater), "stop", AsyncMock(side_effect=stop_polling))
    adapter._escalate_stuck_consumer = Mock()
    app.updater._running = True
    await app.initialize()
    await app.start()
    session_tasks = []
    try:
        await app.update_queue.put(event)
        await _ready(fetched)
        assert app.update_queue.empty()  # fetched, but its real processor still owns the update
        receipt = request_handover(runner, loop)
        assert receipt["accepted"]
        assert runner._external_drain_active
        await _ready(stopped)
        dispatch.set()
        await _ready(popped)
        if phase == "released":
            transport_settle.set()
            await app.update_queue.join()
            # Run the same settlement boundary to its first suspension while the real flush
            # is parked after pop. An empty dictionary must not mean dispatch completed.
            from plugins.platforms.telegram.disconnect_handover import _wait_for_dispatch_settle
            probe = asyncio.create_task(_wait_for_dispatch_settle(adapter, app))
            checked = asyncio.Event()
            loop.call_soon(checked.set)
            await _ready(checked)
            try:
                assert not probe.done()
            finally:
                probe.cancel()
                await asyncio.gather(probe, return_exceptions=True)
        recover.set()
        await _ready(before_admission)
        if phase == "cancelled":
            runner.handover_task.cancel()
            await asyncio.gather(runner.handover_task, return_exceptions=True)
            assert status(runner)["failed"] == "handover cancelled"
            admit.set()
            await _ready(handled)
            await asyncio.gather(*adapter._session_tasks.values())
            assert list(adapter._pending_messages.values()) == [event]
            assert event._drain_deferred
            assert responses == [None] and calls == []
            assert not status(runner)["idle"] and not released.is_set()
            return
        if phase == "released":
            transport_settle.set()
            await _ready(released)
            assert not status(runner)["idle"]  # adapter still owns pre-admission work
            assert runner._draining
        admit.set()
        completed = [asyncio.create_task(executed.wait()), asyncio.create_task(handled.wait())]
        try:
            await asyncio.wait(completed, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            assert executed.is_set(), f"received ask not executed: {responses!r}"
        finally:
            for task in completed:
                task.cancel()
            await asyncio.gather(*completed, return_exceptions=True)
        transport_settle.set()
        await _ready(released)
        assert not status(runner)["idle"]
        # A long old-owner turn must not let the heartbeat restart the intentionally stopped poller.
        await adapter._probe_pending_updates(app.bot, 1)
        await adapter._probe_pending_updates(app.bot, 1)
        adapter._escalate_stuck_consumer.assert_not_called()
        session_tasks = list(adapter._session_tasks.values())
        finish.set()
        await asyncio.gather(*session_tasks)
        await asyncio.wait_for(runner.handover_task, 5)
        assert calls == [("8123", event.text, "secondary" if secondary else None)]
        assert status(runner)["idle"] and not status(runner)["failed"]
        assert release_observations == [(True, True)]
        assert all("draining" not in str(call) for call in adapter.send.call_args_list)
    finally:
        recover.set()
        dispatch.set()
        transport_settle.set()
        admit.set()
        finish.set()
        adapter._drop_delayed_deliveries = True
        app.updater._running = False
        await app.stop()
        tasks = [*adapter._background_tasks, *adapter._session_tasks.values(), getattr(runner, "handover_task", None)]
        tasks += list(adapter._pending_text_batch_tasks.values())
        tasks += list(adapter._pending_photo_batch_tasks.values()) + list(adapter._media_group_tasks.values())
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        await app.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "photo", "album"])
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_failed_popped_batch_cannot_report_successful_idle(kind, failure, monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    adapter = _adapter(runner, monkeypatch)
    event = _event(adapter, kind)
    popped, settle = asyncio.Event(), asyncio.Event()
    adapter._release_platform_lock = lambda: pytest.fail("failed input must retain token ownership")

    async def fail_dispatch(received):
        popped.set()
        await settle.wait()
        raise RuntimeError("batch dispatch failed")

    adapter.handle_message = fail_dispatch
    await _enqueue(adapter, kind, event)
    await _ready(popped)
    batches = [*adapter._pending_text_batch_tasks.values(), *adapter._pending_photo_batch_tasks.values(),
               *adapter._media_group_tasks.values()]
    request_handover(runner, asyncio.get_running_loop())
    try:
        if failure == "cancel":
            batches[0].cancel()
        else:
            settle.set()
        await asyncio.gather(*batches, return_exceptions=True)
        settle.set()  # a cancelled flush may already be owned by the existing held redispatch
        redispatch = adapter._held_inbound_redispatch_task
        if redispatch is not None:
            await asyncio.wait_for(asyncio.shield(redispatch), 5)
        await asyncio.wait_for(runner.handover_task, 5)
        assert status(runner)["failed"]
        assert not status(runner)["idle"]
        assert not status(runner)["token_lock_released"]
        assert adapter._held_inbound_events == [event]
    finally:
        settle.set()
        adapter._drop_delayed_deliveries = True
        tasks = [getattr(runner, "handover_task", None), adapter._held_inbound_redispatch_task]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
