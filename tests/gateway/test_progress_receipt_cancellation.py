"""Regression coverage for progress-send receipts interrupted during turn cleanup."""

import asyncio
import queue
import time
from types import SimpleNamespace

import pytest

from gateway.platforms.base import SendResult
from gateway.progress_events import ProvisionalContentBoundary, RetractedContentBoundary
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
