"""Auto steering retains the original event until an explicit, idempotent queue handoff."""
import asyncio
import re
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def event(text="unrelated question", **kwargs):
    return MessageEvent(text=text, source=SessionSource(
        platform=Platform.TELEGRAM, chat_id="123", user_id="u", chat_type="dm", thread_id="9"),
        message_id=text, **kwargs)


def setup(monkeypatch, redirect=True):
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    runner = GatewayRunner(config=GatewayConfig())
    runner._busy_input_mode = "auto"
    runner._busy_text_mode = "queue"  # auto wins over the legacy text queue setting
    runner._is_user_authorized_for_source = lambda _: True
    runner._admit_bot_message_for_source = lambda _: True
    agent = SimpleNamespace(_current_turn_id="turn", session_id="sid", platform="telegram",
        _supports_active_turn_redirect=True, redirect=Mock(return_value=redirect),
        steer=Mock(return_value=True), interrupt=Mock(), _active_children=[],
        _active_children_lock=threading.Lock())
    runner._session_state("key").turn.agent = agent
    runner._session_state("key").turn.event = event("original")
    adapter = SimpleNamespace(_pending_messages={}, _send_with_retry=AsyncMock())
    runner._delivery_adapter_for = lambda _: adapter
    return runner, agent, adapter


def message_id(agent):
    payload = (agent.steer if agent.steer.called else agent.redirect).call_args.args[0]
    return re.search(r'Auto-steer message_id: ([a-f0-9-]+)', payload).group(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect", [True, False])
async def test_auto_routes_redirect_then_steer_without_replay(monkeypatch, redirect):
    runner, agent, adapter = setup(monkeypatch, redirect)
    assert await runner._handle_active_session_busy_message(event(), "key")
    agent.redirect.assert_called_once()
    assert agent.steer.call_count == (0 if redirect else 1)
    agent.interrupt.assert_not_called()
    assert not adapter._pending_messages
    assert not runner._overflow_queue("key")
    assert message_id(agent)


@pytest.mark.asyncio
async def test_defer_original_once_then_promote_in_arrival_order(monkeypatch):
    from agent.steer_deferral import defer_message
    runner, agent, adapter = setup(monkeypatch)
    first, second = event("first"), event("second")
    await runner._handle_active_session_busy_message(first, "key")
    a = message_id(agent)
    await runner._handle_active_session_busy_message(second, "key")
    b = message_id(agent)
    # Model may defer in reverse order. Queue delivery still follows user arrival order.
    assert (await asyncio.to_thread(defer_message, agent, {"message_id": b}))["accepted"]
    assert (await asyncio.to_thread(defer_message, agent, {"message_id": a}))["accepted"]
    assert (await asyncio.to_thread(defer_message, agent, {"message_id": a}))["status"] == "deferred"
    assert adapter._pending_messages.pop("key") is first
    assert runner._promote_queued_event("key", adapter, None) is second
    assert runner._promote_queued_event("key", adapter, None) is None
    assert first.source.thread_id == "9" and first.message_id == "first"
    assert adapter._send_with_retry.await_count == 2
    assert all("Queued after this" in call.kwargs["content"] for call in adapter._send_with_retry.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_first", [True, False])
async def test_explicit_queue_and_deferred_auto_keep_input_order(monkeypatch, queue_first):
    from agent.steer_deferral import defer_message

    runner, agent, adapter = setup(monkeypatch)
    auto = event("auto follow-up", reply_to_message_id="anchor")
    command = event("/queue explicit follow-up")

    async def queue_explicit():
        await runner._busy_queue_command(command, "key", command.source)

    if queue_first:
        await queue_explicit()
    assert await runner._handle_active_session_busy_message(auto, "key")
    token = message_id(agent)
    if not queue_first:
        await queue_explicit()
    for _ in range(2):
        result = await asyncio.to_thread(defer_message, agent, {"message_id": token})
        assert result["accepted"] and result["message_id"] == token
    head = adapter._pending_messages.pop("key")
    tail = runner._promote_queued_event("key", adapter, None)
    expected = ["explicit follow-up", "auto follow-up"] if queue_first else ["auto follow-up", "explicit follow-up"]
    assert [head.text, tail.text] == expected
    assert (tail if queue_first else head) is auto
    assert auto.reply_to_message_id == "anchor"
    assert runner._promote_queued_event("key", adapter, None) is None
    agent.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_late_steer_finalizer_preserves_the_original_event_and_reply_anchor(monkeypatch):
    from agent.steer_deferral import requeue_undelivered_steers

    runner, agent, adapter = setup(monkeypatch)
    original = event("late input")
    await runner._handle_active_session_busy_message(original, "key")
    wrapper = agent.redirect.call_args.args[0]
    assert await asyncio.to_thread(requeue_undelivered_steers, agent, wrapper) == ""
    assert adapter._pending_messages["key"] is original
    assert original.message_id == "late input" and original.source.thread_id == "9"
    assert not runner._overflow_queue("key")


@pytest.mark.asyncio
async def test_failed_handoff_leaves_steer_actionable(monkeypatch):
    from agent.steer_deferral import defer_message
    runner, agent, adapter = setup(monkeypatch)
    original = event()
    await runner._handle_active_session_busy_message(original, "key")
    token = message_id(agent)
    enqueue = runner._queue_or_replace_pending_event
    runner._queue_or_replace_pending_event = Mock(return_value=False)
    failed = await asyncio.to_thread(defer_message, agent, {"message_id": token})
    assert not failed["accepted"] and "still actionable" in failed["error"]
    assert not adapter._pending_messages
    runner._queue_or_replace_pending_event = enqueue
    assert (await asyncio.to_thread(defer_message, agent, {"message_id": token}))["accepted"]
    assert adapter._pending_messages["key"] is original


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    dict(message_type=MessageType.VOICE, media_urls=["voice.ogg"]),
    dict(message_type=MessageType.PHOTO, media_urls=["photo.jpg"]),
    dict(text="/queue next"), dict(text="/status"), dict(internal=True),
])
async def test_auto_does_not_steer_media_commands_or_internal(monkeypatch, payload):
    runner, agent, adapter = setup(monkeypatch)
    followup = event(**payload)
    await runner._handle_active_session_busy_message(followup, "key")
    agent.redirect.assert_not_called()
    agent.steer.assert_not_called()
    agent.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_later_photo_does_not_merge_into_deferred_original_envelope(monkeypatch):
    from agent.steer_deferral import defer_message

    runner, agent, adapter = setup(monkeypatch)
    original = event("deferred question")
    await runner._handle_active_session_busy_message(original, "key")
    assert (await asyncio.to_thread(defer_message, agent, {"message_id": message_id(agent)}))["accepted"]
    photo = event("photo caption", message_type=MessageType.PHOTO, media_urls=["photo.jpg"])
    assert runner._queue_or_replace_pending_event("key", photo)
    assert adapter._pending_messages.pop("key") is original
    assert original.text == "deferred question" and original.media_urls == []
    assert runner._promote_queued_event("key", adapter, None) is photo
    assert photo.message_id == "photo caption" and photo.media_urls == ["photo.jpg"]
    assert runner._promote_queued_event("key", adapter, None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError],
                         ids=["transport-error", "ack-cancelled"])
async def test_ack_failure_does_not_requeue_twice_or_undo_handoff(monkeypatch, failure_type):
    from agent.steer_deferral import defer_message
    runner, agent, adapter = setup(monkeypatch)
    adapter._send_with_retry.side_effect = failure_type("ack failed")
    original = event()
    await runner._handle_active_session_busy_message(original, "key")
    args = {"message_id": message_id(agent)}
    results = [await asyncio.to_thread(defer_message, agent, args) for _ in range(2)]
    assert adapter._pending_messages["key"] is original
    assert not runner._overflow_queue("key")
    assert all(result["accepted"] for result in results)


@pytest.mark.asyncio
async def test_blocked_ack_can_be_cancelled_after_handoff_without_replaying_drained_original(monkeypatch):
    from agent.steer_deferral import defer_message

    runner, agent, adapter = setup(monkeypatch)
    ack_started = asyncio.Event()
    ack_tasks = []

    async def blocked_send(**kwargs):
        ack_tasks.append(asyncio.current_task())
        ack_started.set()
        await asyncio.Event().wait()

    adapter._send_with_retry.side_effect = blocked_send
    original = event("deferred while transport is blocked", reply_to_message_id="anchor")
    assert await runner._handle_active_session_busy_message(original, "key")
    args = {"message_id": message_id(agent)}
    handoff = asyncio.create_task(asyncio.to_thread(defer_message, agent, args))
    try:
        await asyncio.wait_for(ack_started.wait(), timeout=5)
        accepted = await asyncio.wait_for(asyncio.shield(handoff), timeout=2)
        assert accepted["accepted"]
        assert adapter._pending_messages.pop("key") is original
        assert original.reply_to_message_id == "anchor"
        assert original.source.thread_id == "9"
        assert not runner._overflow_queue("key")
        ack_tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await ack_tasks[0]
        repeated = await asyncio.gather(*(
            asyncio.to_thread(defer_message, agent, args) for _ in range(2)))
        assert all(result["accepted"] for result in repeated)
        assert not adapter._pending_messages
        assert not runner._overflow_queue("key")
        assert adapter._send_with_retry.await_count == 1
    finally:
        for task in ack_tasks:
            task.cancel()
        await asyncio.gather(*ack_tasks, return_exceptions=True)
        await handoff


@pytest.mark.asyncio
async def test_ack_task_registration_failure_keeps_queue_ownership_committed(monkeypatch):
    from agent.steer_deferral import defer_message

    runner, agent, adapter = setup(monkeypatch)
    original = event()
    assert await runner._handle_active_session_busy_message(original, "key")
    # Fail registration of the deferral ack, not the earlier attended-activity task.
    retain_ack = Mock(side_effect=RuntimeError("cannot retain ack"))
    monkeypatch.setattr(runner, "_retain_background_task", retain_ack)
    args = {"message_id": message_id(agent)}
    results = await asyncio.gather(*(
        asyncio.to_thread(defer_message, agent, args) for _ in range(2)))
    assert all(result["accepted"] for result in results)
    assert adapter._pending_messages["key"] is original
    assert not runner._overflow_queue("key")
    retain_ack.assert_called_once()
    ack_task = retain_ack.call_args.args[0]
    await asyncio.gather(ack_task, return_exceptions=True)
    assert ack_task.cancelled()
    adapter._send_with_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_after_admission", [False, True],
                         ids=["stop-before-admission", "stop-during-blocked-ack"])
async def test_stop_fences_deferral_without_waiting_for_ack_or_replaying_original(
        monkeypatch, stop_after_admission):
    from agent.steer_deferral import defer_message
    from gateway.run import _INTERRUPT_REASON_STOP

    runner, agent, adapter = setup(monkeypatch)
    # Keep the real stop, generation, turn-release and pending-message paths. Only
    # external storage, plugin dispatch and background-child control are captured.
    monkeypatch.setattr(runner, "session_store", object(), raising=False)
    monkeypatch.setattr(runner, "_async_session_store", SimpleNamespace(
        _store=runner.session_store,
        get_resume_pending_marker=AsyncMock(return_value=None),
    ), raising=False)
    runner._pause_goal_for_stop = AsyncMock(return_value=False)
    runner._persist_active_agents = Mock()
    runner._evict_cached_agent = Mock()
    adapter.get_pending_message = lambda key: adapter._pending_messages.pop(key, None)
    interrupt_children = Mock()
    monkeypatch.setattr("tools.async_delegation.interrupt_for_session", interrupt_children)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", Mock())
    agent.hard_interrupt = Mock(
        side_effect=lambda *_args, **_kwargs: setattr(agent, "_interrupt_requested", True))
    ack_started = asyncio.Event()
    ack_tasks = []

    async def blocked_send(**kwargs):
        ack_tasks.append(asyncio.current_task())
        ack_started.set()
        await asyncio.Event().wait()

    adapter._send_with_retry.side_effect = blocked_send
    original = event("deferred around stop", reply_to_message_id="anchor")
    assert await runner._handle_active_session_busy_message(original, "key")
    args = {"message_id": message_id(agent)}
    generation = runner._current_session_run_generation("key")
    try:
        if stop_after_admission:
            accepted = await asyncio.wait_for(
                asyncio.to_thread(defer_message, agent, args), timeout=5)
            assert accepted["accepted"]
            await asyncio.wait_for(ack_started.wait(), timeout=5)
            assert adapter._pending_messages["key"] is original

        await asyncio.wait_for(
            runner._busy_stop_command(event("/stop"), "key", original.source), timeout=5)
        agent.hard_interrupt.assert_called_once_with(_INTERRUPT_REASON_STOP)
        agent.interrupt.assert_not_called()
        interrupt_children.assert_called_once_with(
            session_key="key", reason="stop_command", parent_session_id="sid")
        assert runner._current_session_run_generation("key") > generation
        assert runner._session_state("key").turn.agent is None
        assert runner._user_stop_latched("key")
        assert not adapter._pending_messages
        assert not runner._overflow_queue("key")
        stopped = await asyncio.to_thread(defer_message, agent, args)
        assert not stopped["accepted"] and "stopping" in stopped["error"]

        # Cancellation or late completion of transport cannot restore stopped text.
        for task in ack_tasks:
            assert not task.done()
            task.cancel()
        await asyncio.gather(*ack_tasks, return_exceptions=True)
        assert all(task.cancelled() for task in ack_tasks)
        agent._interrupt_requested = False
        retried = await asyncio.to_thread(defer_message, agent, args)
        assert retried["accepted"] is stop_after_admission
        assert not adapter._pending_messages
        assert not runner._overflow_queue("key")
        assert adapter._send_with_retry.await_count == int(stop_after_admission)
    finally:
        for task in ack_tasks:
            task.cancel()
        await asyncio.gather(*ack_tasks, return_exceptions=True)
