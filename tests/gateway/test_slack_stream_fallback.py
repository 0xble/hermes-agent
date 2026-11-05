"""A failed Slack consumer leaves one sealed message for gateway reconciliation."""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.run_turn import GatewayTurnMixin
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from tests.gateway.test_slack_native_streaming import _make_adapter, _open_streams


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_gateway_fallback_reuses_dead_consumers_stream(failure):
    adapter, client = _make_adapter()
    metadata = {"thread_id": "111.000", "user_id": "U123"}
    consumer = GatewayStreamConsumer(adapter, "D1", StreamConsumerConfig(
        transport="auto", chat_type="dm", edit_interval=0.01, buffer_threshold=1, cursor="",
    ), metadata=metadata)
    flushed = asyncio.Event()
    drain = consumer._drain_queue

    def drain_or_fail():
        if failure == "error" and flushed.is_set():
            raise RuntimeError("consumer failed after its preview")
        return drain()

    send_draft = adapter.send_draft

    async def signal_draft(*args, **kwargs):
        result = await send_draft(*args, **kwargs)
        flushed.set()
        return result

    adapter.send_draft = signal_draft
    consumer._drain_queue = drain_or_fail
    task = asyncio.create_task(consumer.run())
    try:
        consumer.on_delta("partial answer")
        await asyncio.wait_for(flushed.wait(), timeout=5)
        if failure == "cancel":
            task.cancel()  # same cancellation as the gateway's bounded flush wait
        await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            await task

    assert not _open_streams(adapter), "dead consumer left a live Slack stream"
    assert consumer.final_content_delivered is False
    assert consumer.message_id == "123.456"
    response = {"final_response": "complete answer with its final footer"}
    turn = SimpleNamespace(
        stream_consumer_holder=[consumer], session_key="slack-turn",
        source=SimpleNamespace(chat_id="D1"),
    )
    await GatewayTurnMixin()._run_agent_mark_streamed_delivery(response, turn)
    if not response.get("already_sent"):
        await adapter.send("D1", response["final_response"], metadata=metadata)

    assert response.get("already_sent") is True
    client.chat_stopStream.assert_awaited_once()
    client.chat_postMessage.assert_not_awaited()
    assert client.chat_update.await_args.kwargs["ts"] == "123.456"
    assert client.chat_update.await_args.kwargs["text"] == response["final_response"]
    assert not _open_streams(adapter)
    assert "_draft_id" not in metadata
