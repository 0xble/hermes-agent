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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "transient", "retry_after"])
async def test_continuous_overflow_events_do_not_starve_admitted_sends(monkeypatch, failure):
    adapter = ReceiptAdapter()
    ctx, runner = _runner(adapter)
    st = runner._progress_edit_state(adapter)
    st.progress_msg_id = "anchor"
    st.progress_lines = ["old"]
    st.retired_progress_lines = 1
    st._PROGRESS_TEXT_LIMIT = 3
    now = [100.0]
    interval = module._PROGRESS_EDIT_INTERVAL
    attempts = []
    accepted = []
    runner._runner._progress_edit_clock = {}
    runner._runner._progress_edit_retry_deadlines = {}
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(runner, "_agent_interrupted", lambda: False)
    monkeypatch.setattr(runner, "_progress_edit_state", lambda _: st)
    ctx.progress_queue.put("new")

    async def edit(chat_id, message_id, content, **kwargs):
        attempts.append((now[0], "edit"))
        return SendResult(success=True, message_id=message_id)

    async def send(chat_id, content, **kwargs):
        attempts.append((now[0], "send"))
        if failure and sum(kind == "send" for _, kind in attempts) == 1:
            return SendResult(success=False, retryable=True,
                              retry_after=2 * interval if failure == "retry_after" else None)
        accepted.append(content)
        return SendResult(success=True, message_id=str(len(attempts)))

    async def sleep(seconds):
        assert seconds > 0
        now[0] += seconds
        if now[0] >= 100 + 6 * interval:
            ctx._run_still_current = lambda: False
        else:
            # A steady event stream keeps the overflow path active, including
            # ticks where the continuation cannot acquire a transport slot.
            ctx.progress_queue.put("new")

    monkeypatch.setattr(adapter, "edit_message", edit)
    monkeypatch.setattr(adapter, "send", send)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    await runner.send_progress_messages()
    assert len(accepted) >= 2, attempts
    assert all(b[0] - a[0] >= interval for a, b in zip(attempts, attempts[1:]))
    if failure == "retry_after":
        assert attempts[2][0] - attempts[1][0] >= 2 * interval
    assert runner._runner._progress_edit_clock[st.edit_clock_key] == attempts[-1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["event", "idle", "legacy_flood"])
async def test_slow_edit_receipt_does_not_charge_another_chat_slot(monkeypatch, path):
    adapter = ReceiptAdapter()
    ctx, runner = _runner(adapter)
    st = runner._progress_edit_state(adapter)
    st.progress_msg_id = "anchor"
    st.progress_lines = ["dirty"]
    now = [100.0]
    interval = module._PROGRESS_EDIT_INTERVAL
    runner._runner._progress_edit_clock = {}
    runner._runner._progress_edit_retry_deadlines = {}
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(runner, "_agent_interrupted", lambda: False)
    monkeypatch.setattr(runner, "_progress_edit_state", lambda _: st)

    async def edit(chat_id, message_id, content, **kwargs):
        now[0] += interval
        if path == "legacy_flood":
            return SendResult(success=False, error="flood control")
        return SendResult(success=True, message_id=message_id)

    async def sleep(seconds):
        ctx._run_still_current = lambda: False

    monkeypatch.setattr(adapter, "edit_message", edit)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    if path == "idle":
        await runner._retry_idle_progress(st)
    else:
        ctx.progress_queue.put("new")
        if path == "legacy_flood":
            # A rejected edit returns directly, so stop after the real transport.
            original_edit = adapter.edit_message
            async def rejected_edit(*args, **kwargs):
                result = await original_edit(*args, **kwargs)
                ctx._run_still_current = lambda: False
                return result
            monkeypatch.setattr(adapter, "edit_message", rejected_edit)
        await runner.send_progress_messages()
    # Separate state sharing the gateway chat clock, as another topic/session does.
    _, sibling_runner = _runner(adapter)
    sibling_runner._runner = runner._runner
    sibling = sibling_runner._progress_edit_state(adapter)
    result = await sibling_runner._send_progress_text(sibling, "another session")
    assert result.success, result
