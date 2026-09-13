"""An unknown send receipt fences every subsequent progress send in the turn."""
import asyncio

import pytest

from gateway.platforms.base import SendResult
from gateway.progress_events import DurableContentBoundary, DurableContentSource
from tests.gateway.test_progress_receipt_cancellation import ReceiptAdapter, _runner


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["raise", "missing"])
@pytest.mark.parametrize("path", ["event", "overflow", "send_only", "stale"])
async def test_ambiguous_receipt_fences_event_overflow_idle_and_boundary(monkeypatch, mode, path):
    monkeypatch.setattr("gateway.run_turn_runner._PROGRESS_EDIT_INTERVAL", 0)
    adapter = ReceiptAdapter(mode)
    ctx, runner = _runner(adapter)
    st = runner._progress_edit_state(adapter)
    if path == "overflow":
        st._PROGRESS_TEXT_LIMIT = 12
    if path == "send_only":
        st.can_edit = False
    if path == "stale":
        st.progress_msg_id = "gone"
        async def stale(**kwargs):
            return SendResult(success=False, error="message to edit not found")
        adapter.edit_message = stale
    texts = ["first-tool", "second-tool", "third-tool"]
    if path == "overflow":
        runner._progress_absorb(st, texts.pop(0))
    for text in texts:
        runner._progress_absorb(st, text)
        try:
            if not await runner._roll_progress_overflow_if_needed(st):
                await runner._progress_send_or_edit(st, text)
        except RuntimeError:
            assert mode == "raise"
    assert len(adapter.sent) == 1
    assert st.cancel_saw_ambiguous_send
    assert st.retired_progress_lines == 0
    assert st.progress_lines == ["first-tool", "second-tool", "third-tool"]
    await runner._retry_idle_progress(st)
    await runner._route_content_boundary(st, DurableContentBoundary(
        boundary_id="content", source=DurableContentSource.STREAM_FINALIZED))
    assert st.cancel_saw_ambiguous_send
    runner._progress_absorb(st, "after-content")
    # Exercise the shared boundary directly as well as its public consumers.
    result = await runner._send_progress_text(st, "suppressed")
    assert not result.success
    await runner._progress_send_or_edit(st, "after-content")
    await runner._drain_progress_on_cancel(st)
    assert len(adapter.sent) == 1
    assert st.retired_progress_lines == 0
    assert ctx._cleanup_msg_ids == []

    # The real queue consumer must respect the same fence on later events.
    if path in {"event", "overflow"}:
        adapter = ReceiptAdapter(mode)
        ctx, runner = _runner(adapter)
        st = runner._progress_edit_state(adapter)
        if path == "overflow":
            st._PROGRESS_TEXT_LIMIT = 12
        monkeypatch.setattr(runner, "_progress_edit_state", lambda adapter: st)
        ctx._run_still_current = lambda: not ctx.progress_queue.empty()
        for text in ["first-tool", "second-tool", "third-tool"]:
            ctx.progress_queue.put(text)
        ctx.progress_queue.put(DurableContentBoundary(
            boundary_id="end", source=DurableContentSource.STREAM_FINALIZED))
        await asyncio.wait_for(runner.send_progress_messages(), 5)
        assert len(adapter.sent) == 1
        assert st.cancel_saw_ambiguous_send


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rejected", "no_id"])
async def test_known_rejection_and_accepted_no_id_do_not_latch_ambiguity(mode):
    adapter = ReceiptAdapter("no_id")
    ctx, runner = _runner(adapter)
    st = runner._progress_edit_state(adapter)
    original = adapter.send
    async def send(**kwargs):
        result = await original(**kwargs)
        if mode == "rejected" and len(adapter.sent) == 1:
            return SendResult(success=False, retryable=True, error="known rejection")
        return result
    adapter.send = send
    runner._progress_absorb(st, "first-tool")
    await runner._progress_send_or_edit(st, "first-tool")
    assert st.retired_progress_lines == (0 if mode == "rejected" else 1)
    runner._progress_absorb(st, "second-tool")
    await runner._progress_send_or_edit(st, "second-tool")
    assert not st.cancel_saw_ambiguous_send
    assert st.retired_progress_lines == 2
    assert [text for _, text in adapter.sent] == [
        "first-tool", "first-tool\nsecond-tool" if mode == "rejected" else "second-tool"]
    await runner._drain_progress_on_cancel(st)
    assert len(adapter.sent) == 2
