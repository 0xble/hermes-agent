"""Buffered progress retries on idle, without bypassing delivery fences."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.platforms.base import SendResult
from gateway.progress_events import ProvisionalContentBoundary
from gateway import run_turn_runner as module
from tests.gateway.test_progress_receipt_cancellation import ReceiptAdapter, _runner


async def _idle_run(monkeypatch, failure, *, overflow=False, fence=None):
    adapter = ReceiptAdapter("no_id" if fence == "accepted_no_id" else "success")
    ctx, runner = _runner(adapter)
    st = runner._progress_edit_state(adapter)
    st.progress_msg_id = "anchor"
    st.progress_lines = ["visible"]
    st.retired_progress_lines = 1
    if fence == "accepted_no_id":
        st.progress_msg_id = None
        st.progress_lines = []
        st.retired_progress_lines = 0
    if overflow:
        st._PROGRESS_TEXT_LIMIT = 24
    now = [100.0]
    initial = now[0]
    interval = module._PROGRESS_EDIT_INTERVAL
    deadline = initial + interval * 2
    shared = {st.edit_clock_key: initial}
    deadlines = {st.edit_clock_key: deadline} if failure == "parked" else {}
    runner._runner._progress_edit_clock = shared
    runner._runner._progress_edit_retry_deadlines = deadlines
    attempts = []
    sleeps = []
    interrupted = [False]
    monkeypatch.setattr(runner, "_agent_interrupted", lambda: interrupted[0])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    ctx.progress_queue.put("new buffered progress")

    async def edit(chat_id, message_id, content, **kwargs):
        attempts.append((now[0], message_id, content))
        if len(attempts) == 1 and failure != "parked":
            return SendResult(success=False, retryable=True, error="temporary rejection",
                              retry_after=interval * 2 if failure == "retry_after" else None)
        return SendResult(success=True, message_id=message_id)

    async def sleep(seconds):
        assert seconds > 0, "idle retry must yield, not spin"
        sleeps.append(seconds)
        now[0] += seconds
        if fence and now[0] >= initial + 0.3:
            if fence == "interrupted":
                interrupted[0] = True
            elif fence == "replaced":
                ctx._run_still_current = lambda: False
            elif fence == "provisional":
                if st.pending_provisional_boundary_id is None:
                    ctx.progress_queue.put(ProvisionalContentBoundary(boundary_id="preview"))
            elif fence == "ambiguous":
                st.cancel_saw_ambiguous_send = True
            elif fence == "cancel":
                raise asyncio.CancelledError
        if now[0] >= initial + interval * 5:
            ctx._run_still_current = lambda: False

    monkeypatch.setattr(adapter, "edit_message", edit)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    # Use the public consumer so cancellation and receipt handling remain in scope.
    monkeypatch.setattr(runner, "_progress_edit_state", lambda adapter: st)
    await runner.send_progress_messages()
    return adapter, ctx, st, attempts, sleeps, initial, deadline


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["parked", "transient", "retry_after"])
@pytest.mark.parametrize("overflow", [False, True])
async def test_dirty_idle_progress_retries_once_accepted(monkeypatch, failure, overflow):
    adapter, ctx, st, attempts, sleeps, initial, deadline = await _idle_run(
        monkeypatch, failure, overflow=overflow)
    assert ctx.progress_queue.empty()
    assert len(attempts) == (1 if failure == "parked" else 2)
    assert attempts[0][0] >= (deadline if failure == "parked" else initial + module._PROGRESS_EDIT_INTERVAL)
    if len(attempts) == 2:
        delay = module._PROGRESS_EDIT_INTERVAL * (2 if failure == "retry_after" else 1)
        assert attempts[1][0] - attempts[0][0] >= delay
    assert st.retired_progress_lines == len(st.progress_lines)
    if overflow:
        assert adapter.sent == [("progress-1", "new buffered progress")]
        assert all(content == "visible" for _, _, content in attempts)
    else:
        assert adapter.sent == []
        assert attempts[-1][2] == "visible\nnew buffered progress"
    assert sleeps  # loop remained idle after success without duplicating delivery


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["interrupted", "replaced", "provisional", "ambiguous", "cancel", "accepted_no_id"])
async def test_idle_retry_respects_turn_and_delivery_fences(monkeypatch, fence):
    adapter, ctx, st, attempts, sleeps, *_ = await _idle_run(monkeypatch, "parked", fence=fence)
    assert attempts == []
    assert adapter.sent == ([("progress-1", "new buffered progress")] if fence == "accepted_no_id" else [])
    assert st.retired_progress_lines == 1
    assert sleeps
