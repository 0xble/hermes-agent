"""Supervisor ticks withhold intermediate chat chrome until the validated final result."""
import asyncio
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


@pytest.mark.asyncio
@pytest.mark.parametrize("display_kind", [None, "supervisor_checkin"])
async def test_intermediate_supervisor_sends_are_suppressed_without_muting_humans(display_kind):
    class Adapter:
        sent = False
        def __init__(self):
            self.calls = []
        async def send(self, *args, **kwargs):
            self.sent = True
            self.calls.append("send")
            return SendResult(success=True, message_id="progress-1")
        async def edit_message(self, *args, **kwargs):
            self.calls.append("edit")
            return SendResult(success=True, message_id="progress-1")
        send_typing = AsyncMock()

    adapter = Adapter()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="7")
    ctx = TurnContext(source=source, progress_queue=queue.Queue(),
                      persist_user_display_kind=display_kind, user_config={"streaming": {"enabled": False}})
    ctx._status_adapter = adapter
    ctx._status_chat_id = source.chat_id
    ctx._run_still_current = lambda: not adapter.sent
    ctx.resolve_display_setting = lambda *args: False
    ctx.interim_assistant_messages_enabled = False
    ctx._progress_metadata = {}
    runner = SimpleNamespace(config=GatewayConfig(), _delivery_adapter_for=lambda _: adapter,
                             _deliver_platform_notice=AsyncMock())
    runner.config.streaming.enabled = False
    turn = TurnRunner(runner, ctx)
    pending = []
    def schedule(coro, *args, **kwargs):
        task = asyncio.create_task(coro)
        pending.append(task)
        return task
    turn._schedule = schedule
    # Real callback path, including the fallback without a stream consumer.
    _, _, interim, _ = turn._setup_stream_consumer("telegram")
    if interim:
        interim("Still examining tool results.")
    turn.progress_callback("subagent.complete", preview="review failed", status="error",
                           child_session_id="child-1", goal="review", summary="review failed",
                           failure_notice_claimed=False)
    if pending:
        await asyncio.gather(*pending)
    if display_kind:
        assert interim is None
        assert not adapter.sent
        runner._deliver_platform_notice.assert_not_awaited()
    else:
        assert adapter.sent
        runner._deliver_platform_notice.assert_awaited_once()
    # Real queue consumer must not create progress bubbles, including queued events.
    adapter.sent = False
    adapter.calls.clear()
    ctx._run_still_current = lambda: len(adapter.calls) < 2
    turn._progress_edit_interval = lambda adapter: 0
    ctx.progress_queue.put("terminal: inspecting last result")
    ctx.progress_queue.put("terminal: checking final artifact")
    await asyncio.wait_for(turn.send_progress_messages(), timeout=3)
    assert adapter.calls == (["send", "edit"] if display_kind is None else [])
    assert ctx.progress_queue.empty()
