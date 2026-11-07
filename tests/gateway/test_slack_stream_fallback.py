"""Dead Slack consumers reconcile only their current, uninterrupted turn's final."""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from tests.gateway.test_gateway_silence_tokens import _runner
from tests.gateway.test_slack_native_streaming import _make_adapter, _open_streams


async def _deliver_gateway_final(monkeypatch, tmp_path, adapter, consumer, response, *, current=True):
    """Run the gateway's suppression/discard and adapter's normal final-send path."""
    runner = _runner(monkeypatch, tmp_path)
    # An established session avoids the unrelated first-contact home-channel notice.
    runner.session_store.load_transcript.return_value = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier reply"},
    ]
    source = SessionSource(platform=Platform.SLACK, chat_id="D1", chat_type="dm",
                           thread_id="111.000", user_id="U123")
    event = MessageEvent(text="question", source=source, message_id="111.001")
    key = "agent:main:slack:dm:D1"
    runner._delivery_adapter_for = lambda source: adapter
    runner._hmwa_runtime_footer_line = lambda *_args: ""
    runner._is_session_run_current = lambda *_args: current
    turn = SimpleNamespace(
        stream_consumer_holder=[consumer], session_key=key, source=source,
        _run_still_current=lambda: current,
    )

    async def completed_agent(**_kwargs):
        await runner._run_agent_mark_streamed_delivery(response, turn)
        return response

    async def handle(event):
        return await runner._handle_message_with_agent(event, source, key, 1)

    runner._run_agent = completed_agent
    adapter.set_message_handler(handle)
    await adapter._process_message_background(event, key)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "error", "stale", "superseded", "interrupted"])
@pytest.mark.parametrize("transformed", [False, True])
async def test_gateway_fallback_reuses_only_current_uninterrupted_stream(monkeypatch, tmp_path, failure, transformed):
    adapter, client = _make_adapter()
    metadata = {"thread_id": "111.000", "user_id": "U123"}
    current = True
    consumer = GatewayStreamConsumer(adapter, "D1", StreamConsumerConfig(
        transport="auto", chat_type="dm", edit_interval=0.01, buffer_threshold=1, cursor="",
    ), metadata=metadata, run_still_current=lambda: current)
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
        if failure == "stale":
            current = False  # /stop or /new invalidates the generation before consumer exit
        elif failure != "error":
            task.cancel()  # same cancellation as the gateway's bounded flush wait
        await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            await task

    assert not _open_streams(adapter), "dead consumer left a live Slack stream"
    assert consumer.final_content_delivered is False
    if failure == "stale":
        assert not consumer._abandoned_draft_message_id
    else:
        assert consumer.message_id == "123.456"
    if failure == "superseded":
        current = False  # ownership can change after cancellation has already sealed the draft
    response = {"final_response": "complete answer with its final footer", "messages": [],
                "response_transformed": transformed}
    if failure == "interrupted":
        response.update(final_response="Operation interrupted.", interrupted=True)
    await _deliver_gateway_final(monkeypatch, tmp_path, adapter, consumer, response, current=current)

    client.chat_stopStream.assert_awaited_once()
    assert not _open_streams(adapter)
    assert "_draft_id" not in metadata
    if failure in {"cancel", "error"}:
        assert response.get("already_sent") is True
        assert not client.chat_postMessage.await_args_list, client.chat_postMessage.await_args_list
        assert client.chat_update.await_args.kwargs["ts"] == "123.456"
        assert client.chat_update.await_args.kwargs["text"] == response["final_response"]
    else:
        assert not response.get("already_sent")
        client.chat_update.assert_not_awaited()
        if not current:
            client.chat_postMessage.assert_not_awaited()
