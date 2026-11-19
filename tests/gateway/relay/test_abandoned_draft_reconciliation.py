"""Relay abandon receipts authorize edits only for the owning consumer draft."""

from types import SimpleNamespace

import pytest

from gateway.run_turn import GatewayTurnMixin
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from tests.gateway.relay.test_relay_live_cards import _connected_adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["owned", "same-thread-other-draft", "single-other-stream", "missing"])
async def test_abandoned_relay_draft_requires_proven_ownership(ownership):
    adapter, stub = _connected_adapter()
    stub.next_draft_result = {"success": True, "message_id": "sealed-stream"}
    consumer = GatewayStreamConsumer(adapter, "D1", StreamConsumerConfig(
        transport="auto", chat_type="dm", cursor="",
    ), metadata={"thread_id": "111.000"})
    await consumer._start_transports()
    draft_id = consumer._draft_id if ownership == "owned" else consumer._draft_id + 1
    if ownership == "missing":
        metadata = {"thread_id": "111.000"}
    else:
        metadata = {"thread_id": "222.000" if ownership == "single-other-stream" else "111.000"}
    await adapter.send_draft("D1", draft_id, "foreground preview", metadata=metadata)
    if ownership == "missing":
        consumer._draft_id = None  # a caller lacking a draft identity must fail closed
    await consumer._abandon_native_stream()
    response = {"final_response": "complete final"}
    turn = SimpleNamespace(
        stream_consumer_holder=[consumer], session_key="relay-turn",
        source=SimpleNamespace(chat_id="D1"), _run_still_current=lambda: True,
    )
    await GatewayTurnMixin()._run_agent_mark_streamed_delivery(response, turn)

    edits = [frame for frame in stub.sent if frame["op"] == "edit"]
    seals = [frame for frame in stub.sent if frame["op"] == "draft" and frame["final"]]
    if ownership == "owned":
        assert response.get("already_sent") is True
        assert len(seals) == 1
        assert len(edits) == 1
        assert edits[0]["message_id"] == "sealed-stream"
        assert edits[0]["content"] == "complete final"
        assert not adapter._open_draft_by_chat
    else:
        assert not response.get("already_sent")
        assert not consumer._abandoned_draft_message_id
        assert not edits
        assert not seals
        assert adapter._open_draft_by_chat
