"""Regression coverage for queued follow-ups at the interrupt recursion cap."""

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
async def test_interrupt_depth_cap_requeues_the_dequeued_event_at_the_head():
    """Real path: A was the slot head and B, C waited in overflow. The drain dequeues A and
    promotes B into the slot, then the depth cap stops recursion. A is the oldest waiting
    message, so it must go back at the head with B and C behind it: nothing dropped, arrival
    order kept. The base code merged A over the slot and silently lost B."""
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._queued_events = {}
    runner._MAX_INTERRUPT_DEPTH = 1
    adapter = SimpleNamespace(_pending_messages={}, _active_sessions={})
    runner._delivery_adapter_for = MagicMock(return_value=adapter)

    event_a, event_b, event_c = (_text_event(t, t.lower()) for t in "ABC")
    adapter._pending_messages[SESSION_KEY] = event_a
    runner._session_state(SESSION_KEY).conversation.queued_events.extend([event_b, event_c])

    # What _run_agent_drain_pending does: dequeue the head, promote the next overflow event.
    dequeued = adapter._pending_messages.pop(SESSION_KEY)
    pending_event = runner._promote_queued_event(SESSION_KEY, adapter, dequeued)
    assert pending_event is event_a and adapter._pending_messages[SESSION_KEY] is event_b

    turn_ctx = SimpleNamespace(
        source=event_a.source,
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
        pending="A",
        pending_event=pending_event,
        response="response",
        result={"interrupted": True, "messages": []},
        stream_task=None,
    )

    assert adapter._pending_messages[SESSION_KEY] is event_a
    assert runner._session_state(SESSION_KEY).conversation.queued_events == [event_b, event_c]
    assert event_a._gateway_accepted is True
