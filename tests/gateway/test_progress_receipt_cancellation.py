"""Regression coverage for progress-send receipts interrupted during turn cleanup."""

import asyncio
import queue
import time
from types import SimpleNamespace

import pytest

from gateway.platforms.base import SendResult
from gateway.progress_events import DurableContentBoundary, DurableContentSource, ProvisionalContentBoundary, RetractedContentBoundary
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext


class ReceiptAdapter:
    """Real TurnRunner boundary with a controllable transport receipt."""

    MAX_MESSAGE_LENGTH = 4000
    message_len_fn = len
    name = "receipt-fake"

    def __init__(self, mode="success"):
        self.mode = mode
        self.sent = []
        self.edits = []
        self.accepted = asyncio.Event()
        self.release = asyncio.Event()
        self.next_id = 0

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.next_id += 1
        message_id = f"progress-{self.next_id}"
        self.sent.append((message_id, content))
        if self.mode == "raise":
            raise RuntimeError("transport failed before receipt")
        if self.mode in {"pending", "pending_raise"}:
            self.accepted.set()
            await self.release.wait()
        if self.mode == "pending_raise":
            raise RuntimeError("transport failed after accepted send")
        return SendResult(success=True, message_id=message_id)

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        self.edits.append((message_id, content))
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None):
        return None


def _runner(adapter, *, cleanup=True):
    source = SimpleNamespace(chat_id="chat", platform="telegram")
    ctx = TurnContext(
        source=source,
        _run_still_current=lambda: True,
        progress_queue=queue.Queue(),
        progress_mode="all",
        progress_grouping="grouped",
        tool_progress_enabled=True,
        _cleanup_progress=cleanup,
    )
    gateway = SimpleNamespace(_adapter_for_source=lambda source: adapter)
    return ctx, TurnRunner(gateway, ctx)


async def _cancel_after_first_send(ctx, runner, adapter):
    ctx.progress_queue.put("tool started")
    task = asyncio.create_task(runner.send_progress_messages())
    await asyncio.wait_for(adapter.accepted.wait(), timeout=5)
    task.cancel()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_accepted_pending_receipt_is_not_resent_on_cancel():
    adapter = ReceiptAdapter("pending")
    ctx, runner = _runner(adapter)

    await _cancel_after_first_send(ctx, runner, adapter)

    assert [content for _, content in adapter.sent] == ["tool started"]
    assert ctx._cleanup_msg_ids == []

    adapter.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ctx._cleanup_msg_ids == ["progress-1"]


@pytest.mark.asyncio
async def test_successful_progress_send_still_tracks_and_cancels_normally():
    adapter = ReceiptAdapter("success")
    ctx, runner = _runner(adapter)
    ctx.progress_queue.put("tool started")

    task = asyncio.create_task(runner.send_progress_messages())
    for _ in range(20):
        if ctx._cleanup_msg_ids:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.wait_for(task, timeout=5)

    assert adapter.sent == [("progress-1", "tool started")]
    assert ctx._cleanup_msg_ids == ["progress-1"]


@pytest.mark.asyncio
async def test_cancel_before_send_does_not_start_a_progress_send():
    adapter = ReceiptAdapter("success")
    ctx, runner = _runner(adapter)
    ctx.progress_queue.put("tool started")
    task = asyncio.create_task(runner.send_progress_messages())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.sent == []
    assert ctx._cleanup_msg_ids == []


@pytest.mark.asyncio
async def test_pending_receipt_exception_never_retries_on_cancel():
    adapter = ReceiptAdapter("pending_raise")
    ctx, runner = _runner(adapter)
    await _cancel_after_first_send(ctx, runner, adapter)
    adapter.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert adapter.sent == [("progress-1", "tool started")]
    assert ctx._cleanup_msg_ids == []


@pytest.mark.asyncio
async def test_late_receipt_cleanup_is_bounded_and_tracks_generation_result(monkeypatch):
    adapter = ReceiptAdapter("pending")
    ctx, runner = _runner(adapter)
    monkeypatch.setattr("gateway.run_turn_runner._PROGRESS_RECEIPT_CANCEL_WAIT_SECONDS", 0.01)
    ctx.progress_queue.put("tool started")

    started = time.monotonic()
    await _cancel_after_first_send(ctx, runner, adapter)
    assert time.monotonic() - started < 2
    assert len(adapter.sent) == 1

    adapter.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ctx._cleanup_msg_ids == ["progress-1"]


@pytest.mark.asyncio
async def test_normal_progress_and_retracted_preview_keep_the_same_anchor():
    adapter = ReceiptAdapter("success")
    ctx, runner = _runner(adapter)
    st = runner._progress_edit_state(adapter)
    st.progress_lines.append("tool started")
    await runner._progress_send_or_edit(st, "tool started")
    original = st.progress_msg_id

    await runner._route_content_boundary(st, ProvisionalContentBoundary(boundary_id="preview"))
    await runner._route_content_boundary(st, RetractedContentBoundary(boundary_id="preview"))
    await runner._drain_progress_on_cancel(st)

    assert original == "progress-1"
    assert st.progress_msg_id == original
    assert adapter.sent == [("progress-1", "tool started")]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["cancel", "durable"])
@pytest.mark.parametrize("failure", ["once", "persistent", "flood", "permanent"])
async def test_transient_overflow_finalization_retries_split_not_oversized_edit(
    monkeypatch, boundary, failure
):
    monkeypatch.setattr("gateway.run_turn_runner._PROGRESS_EDIT_INTERVAL", 0)
    adapter = ReceiptAdapter()
    ctx, runner = _runner(adapter)
    st = runner._progress_edit_state(adapter)
    st._PROGRESS_TEXT_LIMIT = 30
    runner._runner._progress_edit_retry_deadlines = {}
    st.progress_lines.append("initial")
    await runner._progress_send_or_edit(st, "initial")
    original_id = st.progress_msg_id
    # The live loop can absorb the entire queued batch before cancellation interrupts
    # its edit wait. No queued event remains to trigger another overflow attempt.
    lines = ["initial", "first overflow line", "second overflow line"]
    st.progress_lines[:] = lines
    attempts = []

    async def transient_edit(chat_id, message_id, content, **kwargs):
        attempts.append((message_id, content))
        if failure in {"persistent", "flood"} or len(attempts) == 1:
            return SendResult(
                success=False, error="temporary network failure", retryable=True,
                retry_after=60 if failure == "flood" else None,
            )
        if failure == "permanent":
            return SendResult(success=False, error="permission revoked")
        return await ReceiptAdapter.edit_message(adapter, chat_id, message_id, content)

    adapter.edit_message = transient_edit
    if boundary == "cancel":
        await runner._drain_progress_on_cancel(st)
    else:
        await runner._route_content_boundary(
            st, DurableContentBoundary(boundary_id="content", source=DurableContentSource.STREAM_FINALIZED)
        )

    assert ctx.progress_queue.empty()
    # No busy retry on a persistent outage and no API attempt inside a flood wait.
    assert len(attempts) == (1 if failure == "flood" else 2)
    assert {message_id for message_id, _ in attempts} == {original_id}
    assert all(len(text) <= st._PROGRESS_TEXT_LIMIT for _, text in attempts + adapter.sent)
    assert st.can_edit is (failure != "permanent")
    if failure != "once":
        assert adapter.edits == []
        assert adapter.sent == [(original_id, "initial")]
        if boundary == "cancel":
            assert st.progress_msg_id == original_id
            assert st.progress_lines == lines
    else:
        assert adapter.edits == [(original_id, "initial\nfirst overflow line")]
        assert [text for _, text in adapter.sent] == ["initial", "second overflow line"]
        if boundary == "cancel":
            assert st.progress_msg_id == adapter.sent[-1][0]
            assert st.progress_lines == ["second overflow line"]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["none", "retract", "durable", "send_only", "overflow", "edit_failure"])
async def test_queued_progress_batches_preserve_lines_dedup_and_typed_order(monkeypatch, boundary):
    monkeypatch.setattr("gateway.run_turn_runner._PROGRESS_EDIT_INTERVAL", 0)
    adapter = ReceiptAdapter()
    ctx, runner = _runner(adapter)
    st = runner._progress_edit_state(adapter)
    st.progress_lines.append("initial")
    await runner._progress_send_or_edit(st, "initial")
    if boundary == "send_only":
        st.can_edit = False
    if boundary == "overflow":
        st._PROGRESS_TEXT_LIMIT = 90
    if boundary == "edit_failure":
        async def refuse_edit(*args, **kwargs):
            return SendResult(success=False, error="permission revoked")
        adapter.edit_message = refuse_edit
    for n in range(20):
        ctx.progress_queue.put(f"tool {n}")
    ctx.progress_queue.put(("__dedup__", "tool 19", 2))
    if boundary in {"retract", "durable"}:
        ctx.progress_queue.put(ProvisionalContentBoundary(boundary_id="content"))
        ctx.progress_queue.put("deferred tool")
        ctx.progress_queue.put(RetractedContentBoundary(boundary_id="content") if boundary == "retract" else
                               DurableContentBoundary(boundary_id="content", source=DurableContentSource.STREAM_FINALIZED))
        ctx.progress_queue.put("after content")
    current = [True]
    ctx._run_still_current = lambda: current[0]
    async def finish_after_render(state):
        await adapter.send_typing(ctx.source.chat_id)
        current[0] = bool(not ctx.progress_queue.empty() or st.replay_progress_events)

    runner._progress_restore_typing = finish_after_render
    await asyncio.wait_for(runner._progress_loop(st), 3)
    texts = [text for _, text in adapter.edits + adapter.sent]
    for n in range(20):
        assert any(f"tool {n}" in text for text in texts)
    assert any("tool 19 (×3)" in text for text in texts)
    if boundary == "none":
        assert len(adapter.edits) == 1
    elif boundary == "retract":
        assert len(adapter.sent) == 1
        assert "deferred tool" in adapter.edits[-1][1]
        assert "after content" in adapter.edits[-1][1]
    elif boundary == "durable":
        assert "after content" not in adapter.edits[0][1]
        assert len(adapter.sent) == 2
        assert adapter.sent[1][1] == "deferred tool"
    elif boundary == "send_only":
        assert len(adapter.sent) == 22
        assert adapter.edits == []
    elif boundary == "edit_failure":
        assert [text for _, text in adapter.sent[1:]] == [*(f"tool {n}" for n in range(19)), "tool 19 (×3)"]
    else:
        assert all(len(text) <= 90 for text in texts)
