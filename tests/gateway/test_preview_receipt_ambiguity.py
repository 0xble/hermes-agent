"""Preview acceptance is unknown until a receipt, including cleanup and late ACKs."""
import asyncio
import inspect
import queue
from types import SimpleNamespace

import pytest

from gateway.platforms.base import SendResult
from gateway.progress_events import DurableContentBoundary, ProvisionalContentBoundary, RetractedContentBoundary
from gateway.run_turn_runner import TurnRunner
from gateway.stream_consumer import GatewayStreamConsumer, _Tick
from gateway.turn_context import TurnContext


class PreviewTransport:
    MAX_MESSAGE_LENGTH = 4000
    message_len_fn = len
    name = "preview-receipt"

    def __init__(self, outcome, delayed=False):
        self.outcome = outcome
        self.delayed = delayed
        self.accepted = asyncio.Event()
        self.release = asyncio.Event()
        self.sent = []
        self.edits = []
        self.visible = {}

    async def send(self, chat_id, content, **kwargs):
        message_id = f"message-{len(self.sent)}"
        self.sent.append((message_id, content))
        if content == "preview":
            if self.outcome != "refusal":
                self.visible[message_id] = content
            self.accepted.set()
            if self.delayed:
                await self.release.wait()
            if self.outcome == "timeout":
                raise TimeoutError("accepted, ACK lost")
            if self.outcome == "cancel":
                raise asyncio.CancelledError()
            if self.outcome == "missing":
                return None
            if self.outcome == "refusal":
                return SendResult(success=False, error="refused")
        self.visible[message_id] = content
        return SendResult(success=True, message_id=message_id)

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        self.edits.append((message_id, content))
        self.visible[message_id] = content
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id, **kwargs):
        self.visible.pop(message_id, None)
        return True


def setup(outcome, delayed=False):
    adapter = PreviewTransport(outcome, delayed)
    ctx = TurnContext(source=SimpleNamespace(chat_id="chat", platform="telegram"),
                      progress_queue=queue.Queue(), _run_still_current=lambda: True)
    runner = TurnRunner(SimpleNamespace(_adapter_for_source=lambda _: adapter), ctx)
    st = runner._progress_edit_state(adapter)
    st.progress_msg_id = "preceding-progress"
    st.progress_lines = ["earlier-tool"]
    events = []

    def publish(event):
        events.append(event)
        ctx.progress_queue.put(event)
        if isinstance(event, ProvisionalContentBoundary):
            ctx.progress_queue.put("later-tool")

    consumer = GatewayStreamConsumer(adapter, "chat", on_content_boundary=publish)
    return adapter, ctx, runner, st, consumer, events


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "cancel", "missing"])
@pytest.mark.parametrize("cleanup", ["settle", "segment", "silence", "finalize", "cancelled"])
async def test_unknown_preview_never_retracts_or_replays_onto_preceding_progress(monkeypatch, outcome, cleanup):
    monkeypatch.setattr("gateway.run_turn_runner._PROGRESS_EDIT_INTERVAL", 0)
    adapter, ctx, runner, st, consumer, events = setup(outcome)
    try:
        await consumer._first_send("preview", finalize=False)
    except (TimeoutError, asyncio.CancelledError):
        pass
    consumer._accumulated = "preview"
    action = {
        "silence": consumer._suppress_silence_marker,
        "segment": consumer._reset_segment_state,
        "finalize": lambda: consumer._finalize_turn(_Tick(got_done=True)),
        "cancelled": consumer._on_cancelled,
        "settle": consumer._settle_pending_preview_boundary,
    }[cleanup]
    result = action()
    if inspect.isawaitable(result):
        await result
    consumer._settle_pending_preview_boundary()
    await runner._drain_progress_on_cancel(st)
    assert [type(event) for event in events] == [ProvisionalContentBoundary]
    assert consumer._pending_preview_boundary is not None
    assert st.pending_provisional_boundary_id == events[0].boundary_id
    assert list(st.deferred_progress_events) == ["later-tool"]
    assert st.progress_msg_id == "preceding-progress"
    assert not any("later-tool" in text for _, text in adapter.edits)
    assert not consumer.final_content_delivered
    assert not consumer.has_delivered_text("preview")
    assert consumer.delivered_final_matches("preview") is False
    # A retry must not replace an unknown attempt with a refusal or second preview.
    assert not await consumer._first_send("preview", finalize=True)
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "refusal", "timeout", "cancel", "missing"])
@pytest.mark.parametrize("delayed", [False, True, "expiry"])
async def test_preview_receipts_resolve_truthfully_and_once_after_cancellation(monkeypatch, outcome, delayed):
    monkeypatch.setattr("gateway.run_turn_runner._PROGRESS_EDIT_INTERVAL", 0)
    adapter, ctx, runner, st, consumer, events = setup(outcome, delayed)
    task = asyncio.create_task(consumer._first_send("preview", finalize=False))
    await asyncio.wait_for(adapter.accepted.wait(), 5)
    expiries = []
    if delayed == "expiry":
        loop = asyncio.get_running_loop()
        call_later = loop.call_later

        def capture_expiry(delay, callback, *args, **kwargs):
            handle = call_later(delay, callback, *args, **kwargs)
            if consumer._pending_preview_send is not None and callback == consumer._pending_preview_send.cancel:
                expiries.append((handle, callback))
            return handle

        monkeypatch.setattr(loop, "call_later", capture_expiry)
    if delayed:
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    if delayed:
        consumer._settle_pending_preview_boundary()
        await runner._drain_progress_on_cancel(st)
        assert [type(event) for event in events] == [ProvisionalContentBoundary]
        assert list(st.deferred_progress_events) == ["later-tool"]
        if delayed == "expiry":
            assert len(expiries) == 1
            handle, expire = expiries[0]
            handle.cancel()
            expire()
        else:
            adapter.release.set()
        # Wait on the retained receipt, not an arbitrary wall-clock sleep.
        receipt = consumer._pending_preview_send
        await asyncio.gather(receipt, return_exceptions=True)
        await asyncio.sleep(0)  # run its completion callback
    if delayed == "expiry":
        assert consumer._pending_preview_send is None
        assert consumer._preview_send_unknown
        assert [type(event) for event in events] == [ProvisionalContentBoundary]
        assert list(st.deferred_progress_events) == ["later-tool"]
        assert not consumer.has_delivered_text("preview")
        consumer._settle_pending_preview_boundary()
        assert len(events) == 1
        return
    if outcome == "success" and not delayed:
        # Definitive deletion is the control that really does allow retraction.
        await consumer._suppress_silence_marker()
    else:
        consumer._settle_pending_preview_boundary()
    await runner._drain_progress_on_cancel(st)
    phases = [type(event) for event in events]
    if outcome == "refusal" or (outcome == "success" and not delayed):
        assert phases == [ProvisionalContentBoundary, RetractedContentBoundary]
        assert st.progress_msg_id == "preceding-progress"
        assert not adapter.visible.get("message-0")
        assert "later-tool" in adapter.edits[-1][1]
    elif outcome == "success":
        assert phases == [ProvisionalContentBoundary, DurableContentBoundary]
        assert consumer.has_delivered_text("preview")
        assert consumer.delivered_final_matches("preview") is True
        assert consumer.delivered_final_matches("a different final") is False
        assert consumer.message_id == "message-0"
        assert st.progress_msg_id != "preceding-progress"
        assert not any(mid == "preceding-progress" and "later-tool" in text for mid, text in adapter.edits)
        assert [text for _, text in adapter.sent].count("preview") == 1
    else:
        assert phases == [ProvisionalContentBoundary]
        assert list(st.deferred_progress_events) == ["later-tool"]
        assert not consumer.has_delivered_text("preview")
    consumer._settle_pending_preview_boundary()
    assert [type(event) for event in events] == phases
