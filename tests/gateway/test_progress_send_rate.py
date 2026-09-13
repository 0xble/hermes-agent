"""Progress sends obey chat cadence and explicit transport retry outcomes."""
from types import SimpleNamespace

import pytest

from gateway.platforms.base import SendResult
from gateway import run_turn_runner as module
from tests.gateway.test_progress_receipt_cancellation import ReceiptAdapter, _runner


def _clock(monkeypatch, runner):
    now = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    runner._runner._progress_edit_clock = {}
    runner._runner._progress_edit_retry_deadlines = {}
    monkeypatch.setattr(runner, "_agent_interrupted", lambda: False)
    async def sleep(delay):
        assert delay > 0
        now[0] += delay
        if now[0] >= 130:
            runner._ctx._run_still_current = lambda: False
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    return now


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", [False, True, "stale"])
@pytest.mark.parametrize("refuse_middle", [False, True])
async def test_send_only_backlog_uses_shared_chat_cadence(monkeypatch, overflow, refuse_middle):
    adapter = ReceiptAdapter()
    ctx, runner = _runner(adapter)
    now = _clock(monkeypatch, runner)
    ctx.progress_grouping = "separate"
    attempts = []
    accepted = []
    async def send(chat_id, content, **kwargs):
        attempts.append((now[0], content))
        if refuse_middle and len(attempts) == 2:
            return SendResult(success=False, retryable=True, retry_after=12)
        accepted.append(content)
        return SendResult(success=True)  # acceptance requires no editable ID
    monkeypatch.setattr(adapter, "send", send)
    st = runner._progress_edit_state(adapter)
    st.progress_lines = ["one", "two", "six"]
    if overflow:
        st.can_edit = True
        st._PROGRESS_TEXT_LIMIT = 3
    async def deliver(state):
        if overflow:
            if not await runner._roll_progress_overflow_if_needed(state):
                return await runner._progress_send_or_edit(state, None)
            return True
        return await runner._send_unacknowledged_progress(state)
    if overflow == "stale":
        st.progress_msg_id = "gone"
        async def stale(**kwargs):
            return SendResult(success=False, error="message to edit not found")
        monkeypatch.setattr(adapter, "edit_message", stale)
        await deliver(st)
        assert accepted == [] and st.progress_msg_id is None
        now[0] += module._PROGRESS_EDIT_INTERVAL
    await deliver(st)
    assert accepted == ["one"]  # backlog defers instead of waiting in cleanup
    before_cleanup = now[0]
    await runner._drain_progress_on_cancel(st)
    assert accepted == ["one"] and now[0] == before_cleanup
    while accepted != ["one", "two", "six"]:
        assert now[0] < 125
        now[0] += module._PROGRESS_EDIT_INTERVAL
        await deliver(st)
        if refuse_middle and len(attempts) == 2:
            await deliver(st)
            assert len(attempts) == 2
            now[0] += 12
            await deliver(st)
    sibling = runner._progress_edit_state(adapter)
    sibling.progress_lines = ["sibling"]
    assert not await runner._send_unacknowledged_progress(sibling)
    now[0] += module._PROGRESS_EDIT_INTERVAL
    assert await runner._send_unacknowledged_progress(sibling)
    assert [text for _, text in attempts] == ["one", "two", *(["two"] if refuse_middle else []), "six", "sibling"]
    assert all(b[0] - a[0] >= module._PROGRESS_EDIT_INTERVAL for a, b in zip(attempts, attempts[1:]))
    assert st.retired_progress_lines == (1 if overflow else 3)
    assert sibling.retired_progress_lines == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("grouping", ["grouped", "separate"])
@pytest.mark.parametrize("failure", ["retry_after", "transient", "permanent", "typed_rate_limited", "typed_transient", "legacy_rate_limited"])
async def test_idle_send_respects_transport_retry_outcome(monkeypatch, grouping, failure):
    adapter = ReceiptAdapter()
    ctx, runner = _runner(adapter)
    now = _clock(monkeypatch, runner)
    ctx.progress_grouping = grouping
    ctx.progress_queue.put("one buffered line")
    attempts = []
    async def send(chat_id, content, **kwargs):
        attempts.append((now[0], content))
        if len(attempts) == 1:
            return SendResult(success=False, retryable=failure in {"retry_after", "transient"},
                              retry_after=12 if failure == "retry_after" else None,
                              error_kind=failure.removeprefix("typed_") if failure.startswith("typed_") else None,
                              error=("permission denied" if failure == "permanent" else
                                     "too many requests" if failure == "legacy_rate_limited" else "temporary rejection"))
        return SendResult(success=True)
    monkeypatch.setattr(adapter, "send", send)
    st = runner._progress_edit_state(adapter)
    monkeypatch.setattr(runner, "_progress_edit_state", lambda _: st)
    await runner.send_progress_messages()
    assert len(attempts) == (1 if failure == "permanent" else 2)
    if failure != "permanent":
        assert attempts[1][0] - attempts[0][0] >= (12 if failure == "retry_after" else module._PROGRESS_EDIT_INTERVAL)
        assert st.retired_progress_lines == 1
    else:
        assert st.retired_progress_lines == 0  # refusal is not an acceptance receipt
