"""Exercise the real periodic coroutine, Telegram adapter, and turn-final cleanup.

Only the Bot API wire and the 180-second heartbeat clock are fakes.
"""
import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.session_context import get_session_env, set_session_vars, clear_session_vars
from gateway.turn_context import TurnContext
from plugins.platforms.telegram.adapter import TelegramAdapter


class Wire:
    def __init__(self):
        self.messages = {}
        self.sends = []
        self.edits = []
        self.deletes = []
        self.edit_error = None
        self.send_error = None
        self.delete_error = None
        self.accepted = asyncio.Event()
        self.release = None

    async def send_message(self, **kw):
        mid = len(self.sends) + 1
        self.sends.append((mid, kw, get_session_env("HERMES_SESSION_KEY")))
        self.messages[mid] = kw
        self.accepted.set()
        if self.release is not None:
            await self.release.wait()
        if self.send_error:
            raise self.send_error
        return SimpleNamespace(message_id=mid)

    async def do_api_request(self, method, **kw):
        raise AssertionError("Mutable heartbeat must not use rich final transport")

    async def edit_message_text(self, **kw):
        self.edits.append(kw)
        if self.edit_error:
            raise self.edit_error
        mid = kw["message_id"]
        if mid not in self.messages:
            raise BadRequest("Message to edit not found")
        self.messages[mid].update(text=kw["text"])
        return SimpleNamespace(message_id=mid)

    async def delete_message(self, **kw):
        self.deletes.append(kw)
        if self.delete_error:
            raise self.delete_error
        if self.messages.pop(kw["message_id"], None) is None:
            raise BadRequest("Message to delete not found")
        return True


class Clock:
    def __init__(self):
        self.now = 0
        self.ticks = asyncio.Queue()
        self.waiting = asyncio.Event()
        self.intervals = []

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        assert seconds == 180
        self.intervals.append(seconds)
        self.waiting.set()
        await self.ticks.get()
        self.waiting.clear()
        self.now += seconds

    async def step(self):
        self.waiting.clear()
        self.ticks.put_nowait(None)
        await asyncio.wait_for(self.waiting.wait(), 2)


def setup(monkeypatch, *, cleanup=True, topic="22"):
    wire = Wire()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="123:fake", extra={"rich_messages": "always"}))
    adapter._bot = wire
    adapter._send_cooldown_seconds = 0
    adapter._edit_min_interval_seconds = 0
    async def typing(*args):
        pass
    adapter._retrigger_typing = typing
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", thread_id=topic)
    ctx = TurnContext(source=source, session_key=f"topic:{topic}", session_id=f"session:{topic}",
                      run_generation=7, _run_still_current=lambda: True, _cleanup_progress=cleanup,
                      agent_holder=[object()], event_message_id="99")
    ctx._status_thread_metadata = {"thread_id": topic}
    current = [adapter]
    runner = object.__new__(GatewayRunner)
    runner._adapter_for_source = lambda source: current[0]
    runner._should_emit_long_running_notification = lambda *args: True
    runner._agent_activity_summary = lambda agent: {"current_tool": "terminal"}
    turn = TurnRunner(runner, ctx)
    display = SimpleNamespace(_display_surface_mode=lambda *a, **kw: "on", user_config={}, platform_key="telegram",
                              resolve_display_setting=lambda *a, **kw: False)
    clock = Clock()
    import gateway.run_turn as module
    # Module-local clock only; do not replace asyncio globally or the adapter's gate.
    monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(module, "asyncio", _AsyncioClock(clock))
    monkeypatch.setattr("gateway.run._float_env", lambda *a: 180)
    return runner, ctx, turn, adapter, wire, display, clock, current


class _AsyncioClock:
    def __init__(self, clock):
        self.sleep = clock.sleep
    def __getattr__(self, name):
        return getattr(asyncio, name)


async def start(parts):
    runner, ctx, _, _, _, display, clock, _ = parts
    task = asyncio.create_task(runner._run_agent_notify_long_running(display, ctx, [None]))
    await asyncio.wait_for(clock.waiting.wait(), 2)
    return task


async def stop(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def finish(parts):
    runner, ctx, _, adapter, *_ = parts
    result = {}
    runner._run_agent_schedule_bubble_cleanup(result, adapter, ctx)
    callback = adapter.pop_post_delivery_callback(ctx.session_key, generation=ctx.run_generation)
    if callback:
        await callback()


@pytest.mark.asyncio
async def test_multiple_real_ticks_edit_one_anchor_and_final_deletes(monkeypatch):
    p = setup(monkeypatch)
    _, ctx, _, _, wire, _, clock, _ = p
    task = await start(p)
    for _ in range(4):
        await clock.step()
    await stop(task)
    assert len(wire.sends) == 1
    assert len(wire.edits) == 3
    assert "Working — 12 min — terminal" in wire.edits[-1]["text"]
    assert ctx._cleanup_msg_ids == ["1"]
    assert set(clock.intervals) == {180}
    await finish(p)
    assert not wire.messages


@pytest.mark.asyncio
async def test_confirmed_missing_replaces_only_once_and_tracks_both(monkeypatch):
    p = setup(monkeypatch)
    _, ctx, _, _, wire, _, clock, _ = p
    task = await start(p)
    await clock.step()
    wire.messages.clear()
    await clock.step()
    assert len(wire.sends) == 2
    await clock.step()
    assert wire.edits[-1]["message_id"] == 2
    wire.messages.clear()
    await clock.step()
    await clock.step()
    await stop(task)
    assert len(wire.sends) == 2
    assert ctx._cleanup_msg_ids == ["1", "2"]
    await finish(p)
    assert not ctx._status_delivery.cleanup_failures


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [NetworkError("temporary"), BadRequest("not enough rights")])
async def test_edit_failure_never_sends_new_bubble(monkeypatch, error):
    p = setup(monkeypatch)
    wire, clock = p[4], p[6]
    task = await start(p)
    await clock.step()
    wire.edit_error = error
    for _ in range(3):
        await clock.step()
    wire.edit_error = None
    await clock.step()
    await stop(task)
    assert len(wire.sends) == 1
    await finish(p)
    assert not wire.messages


@pytest.mark.asyncio
async def test_429_honors_long_retry_after_without_send_or_edit(monkeypatch):
    p = setup(monkeypatch)
    adapter, wire, clock = p[3], p[4], p[6]
    task = await start(p)
    await clock.step()
    wire.edit_error = RetryAfter(600)
    await clock.step()
    calls = len(wire.edits)
    for _ in range(3):
        await clock.step()
    assert len(wire.edits) == calls
    assert len(wire.sends) == 1
    # Virtual heartbeat time passed; clear the real transport gate explicitly.
    wire.edit_error = None
    adapter._send_cooldown_until.clear()
    await clock.step()
    assert len(wire.edits) == calls + 1
    await stop(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", [True, False])
async def test_cancelled_initial_send_late_receipt_exact_owner(monkeypatch, cleanup):
    p = setup(monkeypatch, cleanup=cleanup)
    ctx, adapter, wire, clock, current = p[1], p[3], p[4], p[6], p[7]
    wire.release = asyncio.Event()
    task = await start(p)
    clock.ticks.put_nowait(None)
    await asyncio.wait_for(wire.accepted.wait(), 2)
    await stop(task)
    await finish(p)
    # Change the current adapter; its unrelated same-id message must survive.
    other = TelegramAdapter(PlatformConfig(enabled=True, token="456:fake"))
    other._bot = Wire()
    other._bot.messages[1] = {"text": "another turn"}
    current[0] = other
    wire.release.set()
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(wire.sends) == 1
    assert bool(wire.messages) is (not cleanup)
    assert other._bot.messages[1]["text"] == "another turn"
    if cleanup:
        assert ctx._cleanup_msg_ids == ["1"]
        assert ctx._status_delivery.owners["1"] is adapter


@pytest.mark.asyncio
async def test_ambiguous_initial_timeout_not_retried_across_ticks(monkeypatch):
    p = setup(monkeypatch)
    wire, clock = p[4], p[6]
    wire.send_error = TimedOut("accepted but receipt lost")
    task = await start(p)
    for _ in range(4):
        await clock.step()
    await stop(task)
    assert len(wire.sends) == 1
    assert len(wire.messages) == 1  # unknown receipt cannot be invented for cleanup


@pytest.mark.asyncio
async def test_topic_context_and_stale_generation_do_not_cross(monkeypatch):
    p = setup(monkeypatch, topic="45")
    ctx, wire, clock = p[1], p[4], p[6]
    tokens = set_session_vars(session_key="wrong-topic", thread_id="999")
    try:
        task = await start(p)
        await clock.step()
        assert get_session_env("HERMES_SESSION_KEY") == "wrong-topic"
        assert wire.sends[0][2] == "topic:45"
        assert wire.sends[0][1]["message_thread_id"] == 45
        ctx._run_still_current = lambda: False
        clock.ticks.put_nowait(None)
        await asyncio.wait_for(task, 2)
        assert len(wire.sends) == 1
    finally:
        clear_session_vars(tokens)


@pytest.mark.asyncio
async def test_failed_final_does_not_cleanup_and_failed_delete_is_reported(monkeypatch, caplog):
    p = setup(monkeypatch)
    runner, ctx, _, adapter, wire, _, clock, _ = p
    task = await start(p)
    await clock.step()
    await stop(task)
    result = {}
    runner._run_agent_schedule_bubble_cleanup(result, adapter, ctx)
    # Delivery failure does not invoke post-send callbacks.
    assert wire.messages and not wire.deletes
    wire.delete_error = BadRequest("not enough rights")
    await adapter.pop_post_delivery_callback(ctx.session_key, generation=ctx.run_generation)()
    assert wire.messages and ctx._status_delivery.cleanup_failures == {"1": "returned_false"}
    assert "1:returned_false" in caplog.text


@pytest.mark.asyncio
async def test_cleanup_retries_scheduler_deferral_without_bypassing_final_priority(monkeypatch):
    p = setup(monkeypatch)
    adapter, wire, clock = p[3], p[4], p[6]
    task = await start(p)
    await clock.step()
    await stop(task)
    # Actual adapter scheduler: no delete request while a final waits.
    adapter._send_final_waiters["123"] = 1
    assert await adapter._delete_status_message("123", "1") is None
    assert not wire.deletes and wire.messages
    async def release_final(seconds):
        assert seconds == 1
        assert not wire.deletes
        adapter._send_final_waiters.clear()
    import gateway.status_delivery as delivery_module
    class RetryClock:
        sleep = staticmethod(release_final)
        def __getattr__(self, name):
            return getattr(asyncio, name)
    monkeypatch.setattr(delivery_module, "asyncio", RetryClock())
    await finish(p)
    assert not wire.messages and len(wire.deletes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [True, False])
async def test_real_background_final_delivery_controls_cleanup_and_releases_hooks(monkeypatch, delivered):
    from gateway.platforms.base import MessageEvent
    p = setup(monkeypatch)
    runner, ctx, _, adapter, wire, _, clock, _ = p
    task = await start(p)
    await clock.step()
    await stop(task)
    runner._run_agent_schedule_bubble_cleanup({}, adapter, ctx)
    released = []
    adapter.register_post_delivery_callback(ctx.session_key, lambda: released.append(True), generation=7)
    async def handler(event):
        return "final response"
    async def chat_action(**kwargs):
        return True
    adapter._message_handler = handler
    wire.send_chat_action = chat_action
    adapter._rich_messages_enabled = False
    if not delivered:
        wire.send_error = BadRequest("not enough rights")
    interrupted = asyncio.Event()
    interrupted._hermes_run_generation = 7
    adapter._active_sessions[ctx.session_key] = interrupted
    event = MessageEvent(text="request", source=ctx.source, message_id="99")
    await adapter._process_message_background(event, ctx.session_key)
    assert released == [True]
    assert (1 not in wire.messages) is delivered
    assert ctx._status_delivery.cleaned is delivered


@pytest.mark.asyncio
async def test_final_cleanup_snapshots_late_id_but_never_new_generation(monkeypatch):
    from gateway.platforms.base import SendResult
    p = setup(monkeypatch)
    runner, ctx, _, adapter, wire, _, clock, _ = p
    task = await start(p)
    await clock.step()
    await stop(task)
    runner._run_agent_schedule_bubble_cleanup({}, adapter, ctx)
    # A genuine late receipt arrived while final delivery was in flight.
    wire.messages[2] = {"text": "late old status"}
    ctx._status_delivery.track(SendResult(success=True, message_id="2"), adapter)
    newer = TurnContext(source=ctx.source, session_key=ctx.session_key, run_generation=8,
                        _cleanup_progress=True, _run_still_current=lambda: True)
    TurnRunner(runner, newer)
    wire.messages[3] = {"text": "new turn status"}
    newer._status_delivery.track(SendResult(success=True, message_id="3"), adapter)
    runner._run_agent_schedule_bubble_cleanup({}, adapter, newer)
    await adapter._fire_post_delivery_callback(ctx.session_key, asyncio.Event(), 7, delivery_succeeded=True)
    assert set(wire.messages) == {3}
    assert adapter.pop_post_delivery_callback(ctx.session_key, generation=8) is not None
