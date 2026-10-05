"""Regression coverage for queued follow-ups at the interrupt recursion cap."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner


SESSION_KEY = "agent:main:telegram:dm:123"


def _text_event(text: str, message_id: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SimpleNamespace(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm"),
        message_id=message_id,
    )


@pytest.mark.asyncio
async def test_interrupt_depth_cap_preserves_existing_text_fifo():
    """A capped follow-up stays behind the occupied slot as its own FIFO turn."""
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._queued_events = {}
    runner._MAX_INTERRUPT_DEPTH = 1
    adapter = SimpleNamespace(_pending_messages={})
    runner._delivery_adapter_for = MagicMock(return_value=adapter)

    callbacks = []
    processed = []
    guard = asyncio.Event()
    setattr(guard, "_hermes_run_generation", 7)
    adapter._active_sessions = {SESSION_KEY: guard}

    def register_post_delivery_callback(session_key, callback, *, generation=None):
        callbacks.append((session_key, callback, generation))

    def finish_session_task(session_key, passed_guard):
        assert passed_guard is guard
        head = adapter._pending_messages.pop(session_key, None)
        if head is not None:
            processed.append(head.text)
        processed.extend(event.text for event in runner._session_state(session_key).conversation.queued_events)
        runner._session_state(session_key).conversation.queued_events.clear()

    adapter.register_post_delivery_callback = register_post_delivery_callback
    adapter._finish_session_task = finish_session_task

    event_a = _text_event("A", "a")
    event_b = _text_event("B", "b")
    adapter._pending_messages[SESSION_KEY] = event_a
    turn_ctx = SimpleNamespace(
        source=event_b.source,
        session_id="session-1",
        session_key=SESSION_KEY,
        run_generation=1,
        _interrupt_depth=1,
        history=[],
        _status_thread_metadata={},
        result_holder=[{"final_response": "done", "messages": []}],
    )

    await GatewayRunner._run_agent_queued_followup(
        runner,
        turn_ctx,
        adapter,
        pending="B",
        pending_event=event_b,
        response="response",
        result={"interrupted": True, "messages": []},
        stream_task=None,
    )

    assert adapter._pending_messages[SESSION_KEY] is event_a
    assert runner._session_state(SESSION_KEY).conversation.queued_events == [event_b]
    assert callbacks and callbacks[0][0] == SESSION_KEY and callbacks[0][2] == 7

    # The post-delivery drain runs both queued events without another inbound message.
    callbacks[0][1]()
    assert processed == ["A", "B"]
    assert SESSION_KEY not in adapter._pending_messages
    assert runner._session_state(SESSION_KEY).conversation.queued_events == []
