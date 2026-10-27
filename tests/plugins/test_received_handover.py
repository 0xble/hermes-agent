"""Received Telegram work survives handover through the real adapter/runner gates."""

import asyncio
import threading
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("telegram")
pytest.importorskip("telegram.ext")

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
@pytest.mark.parametrize("secondary", [False, True], ids=["primary", "secondary"])
@pytest.mark.parametrize("dashboard_drain", [False, True], ids=["no-marker", "dashboard-marker"])
async def test_failed_handover_restores_polling_and_executes_held_input_once(secondary, dashboard_drain, monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    if dashboard_drain:
        from gateway.drain_control import write_drain_request
        write_drain_request(principal="test-dashboard")
    adapter = _adapter(runner, monkeypatch, secondary=secondary)
    event = _event(adapter, "text", secondary=secondary)
    adapter._held_inbound_events.append(event)
    updater = _polling_app(adapter)
    adapter._release_platform_lock = Mock()
    calls, executed = [], asyncio.Event()

    async def execute(received, source, key, generation):
        calls.append((received.message_id, received.text))
        executed.set()
        return "Executed original ask."

    runner._handle_message_with_agent = execute
    request_handover(runner, asyncio.get_running_loop())
    await asyncio.sleep(0)
    await asyncio.wait_for(runner.handover_task, 5)
    assert status(runner)["failed"]
    assert not runner._draining
    assert runner._external_drain_active is dashboard_drain
    assert not adapter._teardown_started
    assert updater.running
    adapter._release_platform_lock.assert_not_called()
    await _ready(executed)
    await adapter._held_inbound_redispatch_task
    tasks = list(adapter._session_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)
    assert calls == [(event.message_id, event.text)]
    assert adapter._held_inbound_events == []
    assert runner._pending_messages == {}
    if dashboard_drain:
        from gateway.drain_control import drain_requested
        assert drain_requested()
        fresh = _event(adapter, "text", secondary=secondary)
        fresh.message_id = "new-after-recovery"
        reply = await runner._handle_message(fresh)
        assert reply is not None and "draining for a maintenance action" in reply
        assert len(calls) == 1
    runner.stop.assert_not_called()


def _polling_app(adapter):
    updater = SimpleNamespace(running=True)

    async def stop():
        updater.running = False

    async def start(**kwargs):
        assert not adapter._teardown_started
        assert kwargs["drop_pending_updates"] is False
        updater.running = True
        return True

    updater.stop = AsyncMock(side_effect=stop)
    adapter._app = SimpleNamespace(updater=updater, update_queue=asyncio.Queue())
    adapter._start_polling_resilient = AsyncMock(side_effect=start)
    return updater


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["held", "cancelled", "partial-release", "partial-conflict"])
async def test_failed_sibling_restores_all_served_pollers(failure, monkeypatch, tmp_path):
    from gateway.status import _get_scope_lock_path, _read_json_file

    runner = _runner(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    first = _adapter(runner, monkeypatch)
    second = _adapter(runner, monkeypatch, secondary=True)
    updaters = [_polling_app(adapter) for adapter in (first, second)]
    for name, adapter in (("first", first), ("second", second)):
        assert adapter._acquire_platform_lock("telegram", name, "test bot")
    nonces = [adapter._platform_lock_nonce for adapter in (first, second)]
    event = _event(second, "text", secondary=True)
    calls, executed, stopping = [], asyncio.Event(), asyncio.Event()

    async def execute(received, source, key, generation):
        calls.append((received.message_id, received.text))
        executed.set()
        return "Executed original ask."

    runner._handle_message_with_agent = execute
    if failure == "held":
        second._held_inbound_events.append(event)
    elif failure == "cancelled":
        await second._app.update_queue.put(event)
        original_stop = updaters[1].stop.side_effect

        async def stop_second():
            await original_stop()
            stopping.set()

        updaters[1].stop.side_effect = stop_second
    else:
        # The first release succeeded before a sibling release raised. The standby
        # cannot have been promoted yet, and rollback must reacquire the first lock.
        fatal = AsyncMock()
        first.set_fatal_error_handler(fatal)

        def fail_release():
            if failure == "partial-conflict":
                from gateway.status import acquire_scoped_lock
                assert acquire_scoped_lock("telegram", "first", metadata={"nonce": "replacement-owner"})[0]
            raise OSError("release failed")

        second._release_platform_lock = Mock(side_effect=fail_release)
    try:
        request_handover(runner, asyncio.get_running_loop())
        await asyncio.sleep(0)
        if failure == "cancelled":
            await _ready(stopping)
            # stop_second has completed before settlement's queue.join suspends.
            await asyncio.sleep(0)
            runner.handover_task.cancel()
            await asyncio.gather(runner.handover_task, return_exceptions=True)
            second._app.update_queue.get_nowait()
            second._app.update_queue.task_done()
        else:
            await asyncio.wait_for(runner.handover_task, 5)
        await asyncio.wait_for(runner._zero_cut_recovery_task, 5)
        assert status(runner)["failed"] and not status(runner)["token_lock_released"]
        assert not runner._draining and not runner._external_drain_active
        for adapter, updater, name, nonce in zip((first, second), updaters, ("first", "second"), nonces):
            if failure == "partial-conflict" and adapter is first:
                assert adapter._teardown_started and not updater.running
                adapter._start_polling_resilient.assert_not_called()
                fatal.assert_awaited_once_with(first)
                assert _read_json_file(_get_scope_lock_path("telegram", name))["metadata"]["nonce"] == "replacement-owner"
            else:
                assert not adapter._teardown_started and updater.running
                adapter._start_polling_resilient.assert_awaited_once()
                assert adapter._platform_lock_identity == name
                assert _read_json_file(_get_scope_lock_path("telegram", name))["metadata"]["nonce"] == nonce
        if failure == "held":
            await _ready(executed)
            await asyncio.gather(*second._session_tasks.values())
            assert calls == [(event.message_id, event.text)]
            assert second._held_inbound_events == []
        runner.stop.assert_not_called()
    finally:
        # Lock I/O is confined to the test's own rendezvous directory.
        from gateway.status_scoped_locks import release_scoped_lock
        for name in ("first", "second"):
            release_scoped_lock("telegram", name)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancelled", "timeout"])
@pytest.mark.parametrize("dashboard_drain", [False, True], ids=["no-marker", "dashboard-marker"])
async def test_uncertain_stop_uses_existing_recovery_without_restarting_old_poller(failure, dashboard_drain, monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    if dashboard_drain:
        from gateway.drain_control import write_drain_request
        write_drain_request(principal="test-dashboard")
    adapter = _adapter(runner, monkeypatch)
    updater = _polling_app(adapter)
    event = _event(adapter, "text")
    adapter._held_inbound_events.append(event)
    calls = []

    async def execute(received, source, key, generation):
        calls.append((received.message_id, received.text))
        return "Executed original ask."

    runner._handle_message_with_agent = execute
    entered, release, recovery_notified = asyncio.Event(), asyncio.Event(), asyncio.Event()
    notified = []

    async def stop():
        entered.set()
        if failure == "error":
            raise RuntimeError("stop failed")
        try:
            await release.wait()
        except asyncio.CancelledError:
            # PTB/httpcore may keep the lifecycle operation alive after its caller
            # timed out. The old fence must remain authoritative until retirement.
            await release.wait()
        updater.running = False

    async def fatal(received):
        notified.append(received)
        recovery_notified.set()

    updater.stop.side_effect = stop
    adapter.set_fatal_error_handler(fatal)
    adapter._release_platform_lock = Mock()
    if failure == "timeout":
        monkeypatch.setattr("plugins.platforms.telegram.adapter._UPDATER_STOP_TIMEOUT", 0.02)
    request_handover(runner, asyncio.get_running_loop())
    await asyncio.sleep(0)
    try:
        await _ready(entered)
        if failure == "cancelled":
            runner.handover_task.cancel()
        await _ready(recovery_notified)
        assert not runner._zero_cut_recovery_task.done()
        assert status(runner)["failed"]
        assert not runner._draining and runner._external_drain_active is dashboard_drain
        assert notified == [adapter]
        assert adapter.fatal_error_code == "telegram_network_error"
        assert adapter.fatal_error_retryable
        assert adapter._teardown_started
        adapter._start_polling_resilient.assert_not_called()
        adapter._release_platform_lock.assert_not_called()
        if failure != "error":
            assert not adapter._handover_polling_stop_task.done()
        assert adapter._held_inbound_events == [event] and calls == []
        # Exercise the existing reconnect publication's held-input transfer without
        # opening a network connection or running the background retry watcher.
        replacement = _adapter(runner, monkeypatch)
        replacement.adopt_held_inbound(adapter)
        replacement._schedule_held_inbound_redispatch()
        await asyncio.wait_for(replacement._held_inbound_redispatch_task, 5)
        await asyncio.gather(*replacement._session_tasks.values())
        await asyncio.wait_for(runner._zero_cut_recovery_task, 5)
        await asyncio.gather(runner.handover_task, return_exceptions=True)
        assert adapter._held_inbound_events == replacement._held_inbound_events == []
        assert calls == [(event.message_id, event.text)]
    finally:
        release.set()
        await asyncio.gather(adapter._handover_polling_stop_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancellation_after_token_transfer_keeps_fence_and_finishes_old_turn(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    adapter = _adapter(runner, monkeypatch)
    updater = _polling_app(adapter)
    released = asyncio.Event()
    adapter._active_sessions["active"] = asyncio.Event()
    adapter._release_platform_lock = released.set
    request_handover(runner, asyncio.get_running_loop())
    await _ready(released)
    runner.handover_task.cancel()
    await asyncio.gather(runner.handover_task, return_exceptions=True)
    assert status(runner)["token_lock_released"] and not status(runner)["failed"]
    assert adapter._teardown_started and not updater.running
    await adapter._probe_pending_updates(SimpleNamespace(), 1)
    await adapter._probe_pending_updates(SimpleNamespace(), 1)
    adapter._start_polling_resilient.assert_not_called()
    runner.stop.assert_not_called()
    adapter._active_sessions.clear()
    await asyncio.wait_for(runner._zero_cut_finish_task, 5)
    await asyncio.wait_for(runner._zero_cut_stop_task, 5)
    runner.stop.assert_awaited_once()


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

    async def restart_polling(**kwargs):
        assert kwargs["drop_pending_updates"] is False
        app.updater._running = True
        return True

    adapter._start_polling_resilient = AsyncMock(side_effect=restart_polling)
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
            assert not runner._draining
            admit.set()
            await _ready(executed)
            finish.set()
            await _ready(handled)
            await asyncio.gather(*adapter._session_tasks.values())
            transport_settle.set()
            await asyncio.wait_for(runner._zero_cut_recovery_task, 5)
            assert adapter._pending_messages == {}
            assert calls == [("8123", event.text, "secondary" if secondary else None)]
            assert responses == ["Executed original ask."]
            assert not status(runner)["idle"] and not released.is_set()
            assert not adapter._teardown_started
            assert app.updater.running
            assert all("draining" not in str(call) for call in adapter.send.call_args_list)
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
    batches = list(adapter._committed_flush_tasks.values())
    assert len(batches) == 1
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


@pytest.mark.asyncio
@pytest.mark.parametrize("secondary", [False, True], ids=["primary", "secondary"])
@pytest.mark.parametrize("failure", ["cancelled", "timeout"])
async def test_pending_ptb_stop_owns_token_until_reconnect_can_start(failure, secondary, monkeypatch, tmp_path):
    """Drive real fatal retirement/reconnect, replacing only network I/O and replay side effects."""
    import contextlib
    import json
    import sys
    import time

    from telegram.ext import Updater
    from gateway.status import _get_scope_lock_path, _read_json_file
    from gateway.status_scoped_locks import release_scoped_lock

    runner = _runner(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr("plugins.platforms.telegram.disconnect_handover.is_zero_cut_handover_enabled", lambda config: True)
    monkeypatch.setattr("plugins.platforms.telegram.adapter._UPDATER_STOP_TIMEOUT", 0.05)
    adapter = _adapter(runner, monkeypatch, secondary=secondary)
    event = _event(adapter, "text", secondary=secondary)
    adapter._held_inbound_events.append(event)
    entered, finish_cleanup, queued, fatal_done = (asyncio.Event() for _ in range(4))
    attempted, executed = asyncio.Event(), asyncio.Event()
    trace, calls, candidates = [], [], []
    updater = Updater(bot=SimpleNamespace(id=123), update_queue=asyncio.Queue())
    updater._running = True
    updater._Updater__polling_task = asyncio.create_task(asyncio.sleep(0))

    async def cleanup():
        # Real PTB stop/_stop_polling, including running=False before this callback.
        # PTB normally installs its final Bot.get_updates call in this slot.
        entered.set()
        try:
            await finish_cleanup.wait()
        except asyncio.CancelledError:
            await finish_cleanup.wait()
        trace.append("old-cleanup-finished")

    updater._Updater__polling_cleanup_cb = cleanup
    adapter._app = SimpleNamespace(updater=updater, update_queue=asyncio.Queue(), bot=SimpleNamespace(),
                                   running=False, shutdown=AsyncMock())
    adapter._set_status_indicator = AsyncMock()
    assert adapter._acquire_platform_lock("telegram", "retirement-test", "isolated test bot")
    nonce = adapter._platform_lock_nonce
    path = _get_scope_lock_path("telegram", "retirement-test")
    runner._failed_platforms, runner._profile_failed_platforms = {}, {}
    runner.delivery_router = SimpleNamespace(adapters=runner.adapters)
    runner._ensure_reconnect_watcher_running = queued.set  # drive watcher passes explicitly below
    runner._update_platform_runtime_status = Mock()
    runner._flag_reconnect_needs_attention = Mock()
    runner._adapter_disconnect_timeout_secs = lambda: 0.02
    runner._platform_connect_timeout_secs = lambda *args, **kwargs: 0.05
    runner._sync_voice_mode_state_to_adapter = Mock()
    runner._bind_voice_input_callback = Mock()
    runner._schedule_planned_restart_replay = Mock()
    runner._start_reconnect_spool_recovery = Mock()
    runner._recover_spool_after_reconnect = AsyncMock()
    runner._redeliver_failed_obligations_for_platform = AsyncMock()
    monkeypatch.setattr("gateway.channel_directory.build_channel_directory", AsyncMock())
    monkeypatch.setattr("gateway.platform_registry.platform_registry.get", Mock())
    monkeypatch.setattr("gateway.run._profile_runtime_scope", lambda *args, **kwargs: contextlib.nullcontext())
    monkeypatch.setattr("gateway.run._reconnect_backoff", lambda attempts: 0.01)
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: tmp_path)
    monkeypatch.setattr("hermes_cli.env_loader.hydrate_profile_secret_sources", lambda home: None)
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: runner.config)

    def retain(task):
        runner._background_tasks.add(task)
        if task.get_name() == "zero-cut-handover":
            runner.handover_task = task
        return task

    runner._retain_background_task = retain

    async def execute(received, source, key, generation):
        calls.append(received)
        executed.set()
        return "Executed original ask."

    runner._handle_message_with_agent = execute

    def wire(candidate, *args):
        candidate.set_message_handler(runner._handle_message)
        if secondary:
            candidate.set_owner_profile("secondary")

    runner._wire_adapter_handlers = wire
    runner._configure_profile_adapter = wire

    def create(platform, config):
        candidate = TelegramAdapter(config)
        candidates.append(candidate)
        candidate.gateway_runner = runner
        candidate.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="reply"))
        candidate.send_typing = AsyncMock()
        candidate.on_processing_complete = AsyncMock()
        candidate._set_status_indicator = AsyncMock()

        async def connect(*, is_reconnect=False):
            assert is_reconnect
            assert candidate._acquire_platform_lock("telegram", "retirement-test", "replacement test bot")
            trace.append("replacement-started")
            candidate._mark_connected()
            return True

        candidate.connect = connect
        attempted.set()
        return candidate

    runner._create_adapter = create
    real_fatal = (runner._make_profile_fatal_error_handler("secondary", Platform.TELEGRAM)
                  if secondary else runner._handle_adapter_fatal_error)

    async def fatal(received):
        await real_fatal(received)
        fatal_done.set()

    adapter.set_fatal_error_handler(fatal)
    request_handover(runner, asyncio.get_running_loop())
    try:
        await _ready(entered)
        assert not updater.running
        if failure == "cancelled":
            runner.handover_task.cancel()
        await _ready(fatal_done)  # actual bounded production retirement has returned
        if not secondary:
            assert queued.is_set() and runner._failed_platforms[Platform.TELEGRAM]["predecessor"] is adapter
        # A foreign PID cannot use same-PID self-reacquisition. Verify real exclusion too.
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            # pytest is intentionally not a gateway command. Identify this known
            # test owner only; PID/start-time checks and lock I/O remain real.
            "import gateway.status as s; import json,os; "
            "from gateway.status_scoped_locks import release_scoped_lock; "
            "s._looks_like_gateway_process=lambda pid: pid==os.getppid(); "
            "ok,_=s.acquire_scoped_lock('telegram','retirement-test',metadata={'nonce':'foreign'}); "
            "print(json.dumps(ok)); "
            "release_scoped_lock('telegram','retirement-test') if ok else None",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, stderr.decode()
        foreign_acquired = json.loads(stdout)
        if not secondary:
            await asyncio.wait_for(runner._reconnect_failed_platform(Platform.TELEGRAM, time.monotonic() + 3600), 5)
        await _ready(attempted)
        record = _read_json_file(path) or {}
        observed = {"stop_pending": not adapter._handover_polling_stop_task.done(),
                    "old_lock_owned": (record.get("metadata") or {}).get("nonce") == nonce,
                    "foreign_acquired": foreign_acquired, "replacement_started": "replacement-started" in trace}
        print(json.dumps(observed, sort_keys=True))
        assert observed == {"stop_pending": True, "old_lock_owned": True,
                            "foreign_acquired": False, "replacement_started": False}
        assert calls == [] and adapter._held_inbound_events == [event]
        assert adapter._polling_teardown_started and not status(runner)["token_lock_released"]
        await adapter._probe_pending_updates(SimpleNamespace(), 1)
        await adapter._probe_pending_updates(SimpleNamespace(), 1)
        finish_cleanup.set()
        await asyncio.wait_for(adapter._handover_polling_stop_task, 5)
        if not secondary:
            # The retained queue remains eligible after the bounded attempt, no manual re-enqueue.
            await asyncio.wait_for(runner._reconnect_failed_platform(Platform.TELEGRAM, time.monotonic() + 3600), 5)
        await _ready(executed)
        replacement = (runner._profile_adapters["secondary"] if secondary else runner.adapters)[Platform.TELEGRAM]
        await asyncio.wait_for(replacement._held_inbound_redispatch_task, 5)
        await asyncio.gather(*replacement._session_tasks.values())
        await asyncio.wait_for(runner._zero_cut_recovery_task, 5)
        assert trace == ["old-cleanup-finished", "replacement-started"]
        assert len(calls) == 1 and calls[0] is event and calls[0].text == "Preserve my original received ask."
        assert event.source._transport_adapter_ref() is replacement
        assert adapter._held_inbound_events == replacement._held_inbound_events == []
        assert status(runner)["failed"] and not status(runner).get("recovering")
        runner.stop.assert_not_called()
    finally:
        finish_cleanup.set()
        await asyncio.gather(adapter._handover_polling_stop_task, return_exceptions=True)
        tasks = [*runner._background_tasks, *getattr(runner, "_fatal_handler_tasks", set()),
                 getattr(adapter, "_platform_lock_retirement_task", None), getattr(runner, "_zero_cut_recovery_task", None)]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        for candidate in candidates:
            await candidate.disconnect()
        release_scoped_lock("telegram", "retirement-test")
