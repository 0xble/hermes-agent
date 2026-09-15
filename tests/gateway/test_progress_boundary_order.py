"""Progress ordering across asynchronous producer and delivery boundaries."""
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway.progress_events import (
    ContentBoundaryBuffer, DurableContentBoundary, DurableContentSource,
    ProvisionalContentBoundary, RetractedContentBoundary,
)
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation_id", [None, "continuation"])
@pytest.mark.parametrize("transition", ["ordinary", "boundary", "cancel"])
@pytest.mark.parametrize("first_id", [None, "predecessor"])
@pytest.mark.parametrize("edit_failure", [False, True])
async def test_overflow_continuation_never_reuses_predecessor_identity(
    monkeypatch, continuation_id, transition, first_id, edit_failure,
):
    import asyncio
    import queue

    from gateway import run_turn_runner as module
    from gateway.platforms.base import SendResult
    from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter

    done = False
    cancelled = False
    now = [100.0]

    class Adapter(ProgressCaptureAdapter):
        MAX_MESSAGE_LENGTH = 100 if edit_failure else 10

        async def send(self, chat_id, content, **kwargs):
            nonlocal done
            self.sent.append(content)
            done = "end" in content
            message_id = (first_id if len(self.sent) == 1 else
                          continuation_id if len(self.sent) == 2 else
                          "terminal" if continuation_id else None)
            return SendResult(success=True, message_id=message_id)

        async def edit_message(self, chat_id, message_id, content):
            nonlocal done
            self.edits.append((message_id, content))
            done = "end" in content
            if edit_failure:
                return SendResult(success=False, error="message to edit not found")
            return SendResult(success=True, message_id=message_id)

    async def advance_clock(delay):
        nonlocal cancelled
        now[0] += max(delay, 2.0)
        assert now[0] < 130, "progress loop did not finish"
        if transition == "cancel" and "bbbbbb" in adapter.sent and not cancelled:
            cancelled = True
            raise asyncio.CancelledError

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(module.asyncio, "sleep", advance_clock)
    adapter = Adapter()
    pending = queue.Queue()
    for line in ("aaaaaa", "bbbbbb"):
        pending.put(line)
    if transition != "ordinary":
        pending.put(DurableContentBoundary("content", DurableContentSource.STREAM_FINALIZED))
    pending.put("end")
    ctx = SimpleNamespace(
        source=SimpleNamespace(chat_id="chat"), progress_queue=pending,
        progress_grouping="grouped", _native_slack_task_cards=False,
        _progress_metadata=None, _progress_reply_to=None, _cleanup_progress=False,
        agent_holder=[None], _run_still_current=lambda: not done,
        last_progress_msg=[None], repeat_count=[0],
    )
    runner = module.TurnRunner(SimpleNamespace(_adapter_for_source=lambda source: adapter), ctx)

    await runner.send_progress_messages()

    expected_edits = [("predecessor", "aaaaaa\nbbbbbb" if edit_failure else "aaaaaa")] if first_id else []
    if first_id and continuation_id and not edit_failure:
        expected_edits.append(("continuation", "bbbbbb\nend" if transition == "ordinary" else "bbbbbb"))
        if transition == "cancel":
            expected_edits.append(("terminal", "end"))
    assert adapter.edits == expected_edits
    assert adapter.sent == (["aaaaaa", "bbbbbb"] if first_id and continuation_id and transition == "ordinary" and not edit_failure
                            else ["aaaaaa", "bbbbbb", "end"])
    assert cancelled == (transition == "cancel")


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", [False, True])
@pytest.mark.parametrize("error", ["permission denied", "read timeout"])
async def test_unaccepted_progress_without_id_remains_buffered(overflow, error):
    import queue

    from gateway.run_turn_runner import TurnRunner
    from gateway.platforms.base import SendResult
    from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter

    adapter = ProgressCaptureAdapter()
    failed = SendResult(success=False, error=error)
    accepted = SendResult(success=True)
    adapter.send = AsyncMock(side_effect=([accepted, failed, accepted, accepted] if overflow
                                         else [failed, accepted]))
    ctx = SimpleNamespace(
        source=SimpleNamespace(chat_id="chat"), progress_queue=queue.Queue(),
        progress_grouping="grouped", _progress_metadata=None,
        _progress_reply_to=None, _cleanup_progress=False,
    )
    runner = TurnRunner(None, ctx)
    state = runner._progress_edit_state(adapter)
    state.progress_lines = ["aaaaaa", "bbbbbb", "cccccc"] if overflow else ["bbbbbb"]
    if overflow:
        state._PROGRESS_TEXT_LIMIT = 10
        await runner._roll_progress_overflow_if_needed(state)
    else:
        await runner._progress_send_or_edit(state, "bbbbbb")

    assert state.progress_lines == (["bbbbbb", "cccccc"] if overflow else ["bbbbbb"])
    runner._progress_absorb(state, "end")
    await runner._progress_send_or_edit(state, "end")
    assert [call.kwargs["content"] for call in adapter.send.await_args_list] == (
        ["aaaaaa", "bbbbbb", "bbbbbb", "cccccc\nend"] if overflow else ["bbbbbb", "bbbbbb\nend"]
    )
    assert state.progress_lines == []


def test_overlapping_preview_outcomes_preserve_progress_order():
    buffer = ContentBoundaryBuffer()
    first = ProvisionalContentBoundary("first")
    second = ProvisionalContentBoundary("second")
    durable = DurableContentBoundary("second", DurableContentSource.STREAM_FINALIZED)
    assert buffer.feed("before") == ["before"]
    for item in [first, "middle", second, "after", durable, durable]:
        assert buffer.feed(item) == []
    assert buffer.feed(RetractedContentBoundary("first")) == ["middle", durable, "after"]
    assert buffer.feed(RetractedContentBoundary("first")) == []
    assert buffer.finish() == []


@pytest.mark.asyncio
async def test_worker_queues_multiple_segments_before_platform_delivery():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(side_effect=[
        SimpleNamespace(success=True, message_id="first"),
        SimpleNamespace(success=True, message_id="second"),
    ])
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True))
    events = []
    consumer = GatewayStreamConsumer(adapter, "chat", StreamConsumerConfig(),
        on_content_boundary=events.append)
    consumer.on_delta("first segment")
    consumer.on_segment_break()
    consumer.on_delta("second segment")
    consumer.finish()
    provisional = list(events)
    assert all(isinstance(item, ProvisionalContentBoundary) for item in provisional)
    assert len(provisional) == 2
    await consumer.run()
    durable = [item for item in events if isinstance(item, DurableContentBoundary)]
    assert [item.boundary_id for item in durable] == [item.boundary_id for item in provisional]
    assert [item.message_id for item in durable] == ["first", "second"]
    assert [call.kwargs["content"] for call in adapter.send.call_args_list] == [
        "first segment", "second segment"]


def test_dedup_after_a_durable_seal_becomes_first_line():
    from gateway.run_turn_runner import TurnRunner
    state = SimpleNamespace(progress_lines=[])
    text = TurnRunner._progress_absorb(None, state, ("__dedup__", "same tool", 1))
    assert state.progress_lines == [text] == ["same tool"]


@pytest.mark.asyncio
async def test_ambiguous_commentary_is_not_declared_deleted():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=False,
        error="read timeout", retryable=False))
    events = []
    consumer = GatewayStreamConsumer(adapter, "chat", StreamConsumerConfig(),
        on_content_boundary=events.append)
    consumer.on_commentary("possibly delivered")
    consumer.finish()
    await consumer.run()
    assert len(events) == 2
    assert isinstance(events[0], ProvisionalContentBoundary)
    assert isinstance(events[1], DurableContentBoundary)
    assert events[0].boundary_id == events[1].boundary_id


@pytest.mark.asyncio
async def test_persistent_draft_releases_live_progress_before_turn_end():
    from tests.gateway.test_stream_consumer_draft import _make_draft_capable_adapter
    adapter = _make_draft_capable_adapter()
    adapter.draft_stream_is_message = True
    buffer = ContentBoundaryBuffer()
    ready = []
    consumer = GatewayStreamConsumer(adapter, "chat", StreamConsumerConfig(
        transport="draft", chat_type="dm", cursor=""),
        on_content_boundary=lambda event: ready.extend(buffer.feed(event)))
    consumer.on_delta("first segment")
    consumer._drain_queue()
    await consumer._start_transports()
    assert await consumer._send_or_edit("first segment")
    assert buffer.feed("tool started") == ["tool started"]
    consumer.on_segment_break()
    consumer.on_delta(" continued")
    tick = consumer._drain_queue()
    await consumer._end_segment(tick)
    consumer._drain_queue()
    assert await consumer._send_or_edit("first segment continued")
    assert len([event for event in ready if isinstance(event, DurableContentBoundary)]) == 1
    assert consumer._accumulated == "first segment continued"


@pytest.mark.asyncio
async def test_progress_cleanup_failure_cannot_leak_the_session_slot():
    import asyncio
    from gateway.run import GatewayRunner
    started = asyncio.Event()
    async def failing_drain():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("transport disconnected")
    progress = asyncio.create_task(failing_drain())
    tracking = asyncio.create_task(asyncio.Event().wait())
    await started.wait()
    runner = object.__new__(GatewayRunner)
    runner._release_running_agent_state = MagicMock()
    runner._draining = False
    ctx = SimpleNamespace(stream_consumer_holder=[None], session_key="owned",
        streaming_tts_consumer_holder=[None], run_generation=1)
    await runner._run_agent_cleanup_turn_tasks(ctx, progress_task=progress,
        log_task=None, interrupt_monitor=None, _notify_task=None,
        tracking_task=tracking, stream_task=None)
    assert tracking.cancelled()
    runner._release_running_agent_state.assert_called_once_with("owned", run_generation=1)
