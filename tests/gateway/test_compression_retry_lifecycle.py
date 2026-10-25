"""A compression retry is the same admitted input, not another completed turn."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.bot_loop_guard import BotLoopGuard, BotLoopGuardSettings
from gateway.platforms.base import MessageType, ProcessingOutcome
from hermes_state import SessionDB
from tests.gateway.test_session_hygiene import _make_cooldown_runner
from tests.waiting import async_wait_until


@pytest.fixture
def locked_lane(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "retry-lifecycle"
    db.create_session(sid, "telegram")
    runner, adapter, event = _make_cooldown_runner(monkeypatch, tmp_path, MagicMock, db, sid)
    key = runner.session_store.get_or_create_session.return_value.session_key
    monkeypatch.setattr(runner, "_lookup_session_id_under_store_lock", lambda *_: sid)
    runner._handle_message_with_agent = AsyncMock(return_value="actual reply")
    runner._run_post_turn_hooks = AsyncMock()
    adapter._start_typing_refresh = MagicMock(return_value=None)
    adapter._stop_typing_refresh = AsyncMock()
    adapter._fire_post_delivery_callback = AsyncMock()
    assert db.try_acquire_compression_lock(sid, "worker")
    yield runner, adapter, event, key, db, sid
    db.release_compression_lock(sid, "worker")
    db.close()


@pytest.mark.asyncio
async def test_outbox_admission_stays_pending_across_compression_retries(locked_lane, tmp_path):
    from gateway.outbox import Outbox, event_kind, transport_id

    runner, adapter, event, key, db, sid = locked_lane
    # Direct dispatch has an existing adapter owner, just as native retries do.
    adapter._active_sessions[key] = asyncio.Event()
    runner.config.durable_outbox_enabled = True
    runner._resolve_profile_home_for_source = lambda *_: tmp_path
    store = Outbox(tmp_path)
    event_id = transport_id(event)
    assert event_id is not None
    turn_id = None
    for _ in range(3):
        assert await runner._handle_message(event) is None
        turn_id = turn_id or event._outbox_turn_id
        assert event._outbox_turn_id == turn_id
        assert store.lookup("default", "telegram", event_id, event_kind(event)) == (turn_id, None)
    db.release_compression_lock(sid, "worker")
    assert await runner._handle_message(event) == "actual reply"
    assert store.lookup("default", "telegram", event_id, event_kind(event)) == (turn_id, "completed")
    runner._handle_message_with_agent.assert_awaited_once()


@pytest.mark.parametrize("boundary", ["internal", "control", "bot", "relay", "loop", "command"])
def test_text_debounce_preserves_ingress_boundaries(locked_lane, boundary):
    _runner, adapter, human, _key, _db, _sid = locked_lane
    other = replace(human, text="another input", message_id="2")
    assert adapter._can_merge_text_debounce_events(human, other)
    if boundary == "internal":
        other.internal = True
    elif boundary == "control":
        other.allow_gateway_control = False
    elif boundary == "bot":
        other.source = replace(human.source, is_bot=True)
    elif boundary == "relay":
        other.metadata = {"notification_origin": "relay_delivery"}
    elif boundary == "loop":
        other.metadata = {"loop_wakeup": True}
    else:
        other.text = "/stop"
    assert not adapter._can_merge_text_debounce_events(human, other)
    assert not adapter._can_merge_text_debounce_events(other, human)


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", ["budget", "rewrite"])
async def test_retry_admits_native_input_once(locked_lane, monkeypatch, probe):
    runner, adapter, event, key, db, sid = locked_lane
    adapter._active_sessions[key] = asyncio.Event()
    event.source.is_bot = True
    runner._bot_loop_guard = BotLoopGuard(settings=lambda: BotLoopGuardSettings())
    # Keep the real authorization cooldown peek and real budget accounting.
    del runner._is_user_authorized
    runner._principal_authorized = lambda *_a, **_k: True
    runner._scale_to_zero_note_real_inbound = MagicMock()
    rewrites = []

    async def hook(name, **kwargs):
        if name == "pre_gateway_dispatch" and probe == "rewrite":
            rewrites.append(kwargs["event"].text)
            return [{"action": "rewrite", "text": "prefix:" + kwargs["event"].text}]
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", hook)
    for _ in range(25):
        assert await runner._handle_message(event) is None
        pending = adapter._pending_messages.pop(key, None)
        if probe == "budget":
            assert pending is event
    if probe == "budget":
        assert not runner._bot_loop_guard.blocked(runner._bot_loop_guard_conversation(event.source))
    else:
        assert rewrites == ["hello"]
        assert event.text == "prefix:hello"
        runner._scale_to_zero_note_real_inbound.assert_called_once_with()
    db.release_compression_lock(sid, "worker")
    assert await runner._handle_message(event) == "actual reply"
    runner._handle_message_with_agent.assert_awaited_once()
    runner._run_post_turn_hooks.assert_awaited_once()
    # A genuinely new bot event still spends the budget, and a tripped cooldown
    # still refuses a previously admitted input on its next attempt.
    for i in range(20):
        await runner._hm_admit_event(replace(event, message_id=f"new-{i}"))
    assert runner._bot_loop_guard.blocked(runner._bot_loop_guard_conversation(event.source))
    assert await runner._hm_admit_event(event) is None


@pytest.mark.asyncio
async def test_retry_has_one_pending_adapter_lifecycle(locked_lane):
    runner, adapter, event, key, db, sid = locked_lane
    adapter.on_processing_start = AsyncMock()
    adapter.on_processing_complete = AsyncMock()
    dispatched = []
    real_handler = runner._handle_message

    async def handle(inbound):
        dispatched.append(inbound)
        return await real_handler(inbound)

    adapter.set_message_handler(handle)
    try:
        await adapter.handle_message(event)
        await async_wait_until(lambda: len(dispatched) >= 4, timeout=10)
        assert all(item is event for item in dispatched)
        adapter.on_processing_start.assert_awaited_once_with(event)
        adapter.on_processing_complete.assert_not_awaited()
        assert adapter._start_typing_refresh.call_count == 1
        adapter._fire_post_delivery_callback.assert_not_awaited()
        runner._handle_message_with_agent.assert_not_awaited()
        assert adapter.sent == []
        db.release_compression_lock(sid, "worker")
        await async_wait_until(lambda: bool(adapter.on_processing_complete.await_count), timeout=10)
        adapter.on_processing_complete.assert_awaited_once_with(event, ProcessingOutcome.SUCCESS)
        assert [sent["content"] for sent in adapter.sent] == ["actual reply"]
        runner._handle_message_with_agent.assert_awaited_once()
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["queue", "interrupt"])
@pytest.mark.parametrize("head_kind", ["wake", "photo"])
async def test_deferred_head_cannot_absorb_same_sender_text(locked_lane, mode, head_kind):
    runner, adapter, event, key, db, sid = locked_lane
    adapter._active_sessions[key] = asyncio.Event()
    adapter._busy_text_mode = mode
    runner._busy_input_mode = "queue"
    runner._effective_busy_text_mode = lambda *_: mode
    adapter._busy_session_handler = runner._handle_active_session_busy_message
    head = replace(event, internal=True, text="process finished", message_id="wake",
                   metadata={"notification_category": "process"})
    if head_kind == "photo":
        head = replace(event, message_type=MessageType.PHOTO, text="photo caption", message_id="photo")
    head_text, head_metadata = head.text, dict(head.metadata)
    human = replace(event, text="human instruction", message_id="human")
    try:
        assert await runner._handle_message(head) is None
        assert adapter._pending_messages[key] is head
        # The lock can commit before the retained head's retry backoff expires.
        db.release_compression_lock(sid, "worker")
        await adapter._handle_message_while_active(human, key)
        await adapter._flush_text_debounce_now(key)
        assert adapter._pending_messages[key] is head
        assert head.text == head_text and head.metadata == head_metadata
        assert human.text == "human instruction" and not human.internal
        assert runner._overflow_queue(key) == [human]
        assert key not in adapter._text_debounce_store()
    finally:
        adapter._discard_text_debounce(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,lock_boundary", [
    ("queue", "retained"), ("interrupt", "retained"), ("queue", "buffered"),
])
@pytest.mark.parametrize("third_kind", ["human", "relay", "internal"])
async def test_later_inputs_follow_retained_retry(locked_lane, mode, lock_boundary, third_kind):
    runner, adapter, event, key, db, sid = locked_lane
    adapter._busy_text_mode = mode
    runner._busy_input_mode = "queue"
    runner._effective_busy_text_mode = lambda *_: mode
    adapter._busy_session_handler = runner._handle_active_session_busy_message
    adapter.on_processing_start = AsyncMock()
    executed = []
    model_started, model_release = asyncio.Event(), asyncio.Event()

    from gateway.turn_context import TurnContext

    async def model_turn(*, message, source, session_id, session_key, run_generation,
                         _interrupt_depth=0, **_kwargs):
        if lock_boundary == "buffered" and message == event.text:
            model_started.set()
            await model_release.wait()
        executed.append(message)
        result = {"final_response": message, "messages": []}
        context = TurnContext(
            source=source, session_id=session_id, session_key=session_key,
            run_generation=run_generation, history=[], _interrupt_depth=_interrupt_depth,
        )
        context.result_holder[0] = result
        # Preserve production's dequeue, successor promotion and in-band recursion.
        # Re-entering cold ingress for each follower would manufacture an orphan.
        pending_event, pending = await runner._run_agent_drain_pending(
            result, adapter, source, session_key)
        if pending_event or pending:
            return await runner._run_agent_queued_followup(
                context, adapter, pending, pending_event, result, result, None)
        return result

    async def execute(inbound, source, session_key, run_generation):
        result = await runner._run_agent(
            message=inbound.text, source=source, session_id=sid,
            session_key=session_key, run_generation=run_generation)
        return result["final_response"]

    runner._run_agent = AsyncMock(side_effect=model_turn)
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(
        side_effect=lambda **kwargs: kwargs["event"].text)
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._persist_prompt_pins = AsyncMock()
    runner._handle_message_with_agent.side_effect = execute
    adapter.set_message_handler(runner._handle_message)
    try:
        if lock_boundary == "buffered":
            db.release_compression_lock(sid, "worker")
        await adapter.handle_message(event)
        if lock_boundary == "buffered":
            await async_wait_until(model_started.is_set, timeout=10)
        else:
            await async_wait_until(lambda: adapter._requeue_counts.get(key, 0) >= 2, timeout=10)
        owner = adapter._session_tasks[key]
        second = replace(event, text="second", message_id="2")
        third = replace(event, text="third", message_id="3")
        if third_kind == "relay":
            third.text += "\n[relay from=hermes:default/canary-source receipt=canary-C]"
        elif third_kind == "internal":
            third.internal = True
            third.metadata = {"notification_category": "process"}
        expected = [event.text, second.text, third.text]
        await adapter.handle_message(second)
        if lock_boundary == "buffered":
            assert adapter._text_debounce_store()[key].event is second
            assert db.try_acquire_compression_lock(sid, "worker")
        await adapter.handle_message(third)
        await adapter._flush_text_debounce_now(key)
        if lock_boundary == "buffered":
            assert adapter._pending_messages[key] is second
            assert runner._overflow_queue(key) == [third]
            assert key not in adapter._text_debounce_store()
        else:
            assert adapter._pending_messages[key] is event
        assert event.text == "hello" and event.message_id == "1"
        assert adapter._session_tasks[key] is owner and not owner.done()
        adapter.on_processing_start.assert_awaited_once_with(event)
        assert adapter.sent == []
        db.release_compression_lock(sid, "worker")
        model_release.set()
        await async_wait_until(lambda: len(executed) == len(expected), timeout=10)
        assert executed == expected
        await async_wait_until(lambda: key not in adapter._active_sessions, timeout=10)
        assert [sent["content"] for sent in adapter.sent] == expected
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_cancelling_retained_retry_does_not_resurrect_owner(locked_lane, shutdown):
    runner, adapter, event, key, _db, _sid = locked_lane
    adapter.on_processing_start = AsyncMock()
    adapter.on_processing_complete = AsyncMock()
    adapter.set_message_handler(runner._handle_message)
    preserved = []
    if shutdown:
        from types import SimpleNamespace

        def flush(pending_key, pending, *, reason):
            assert reason == "adapter_shutdown"
            preserved.append((pending_key, pending))
            adapter._pending_messages.pop(pending_key)

        adapter.gateway_runner = SimpleNamespace(_flush_owned_pending=flush)
    try:
        await adapter.handle_message(event)
        await async_wait_until(lambda: adapter._requeue_counts.get(key, 0) >= 2, timeout=10)
        owner = adapter._session_tasks[key]
        assert adapter._pending_messages[key] is event
        if shutdown:
            await adapter.cancel_background_tasks()
            assert preserved == [(key, event)]
        else:
            await adapter.cancel_session_processing(key)
        assert owner.done()
        assert key not in adapter._session_tasks
        assert key not in adapter._pending_messages
        assert key not in adapter._active_sessions
        assert not any(not task.done() for task in adapter._background_tasks)
        adapter.on_processing_start.assert_awaited_once_with(event)
        adapter.on_processing_complete.assert_awaited_once_with(event, ProcessingOutcome.CANCELLED)
        adapter._fire_post_delivery_callback.assert_not_awaited()
        runner._handle_message_with_agent.assert_not_awaited()
        assert adapter.sent == []
    finally:
        await adapter.cancel_background_tasks()
