import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, SendResult
from gateway.review_status import ReviewStatuses
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


def manager(tmp_path):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True, message_id="7")),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter, _thread_metadata_for_source=lambda _: {})
    statuses = runner._review_statuses = ReviewStatuses(runner, home=tmp_path)
    return source, adapter, runner, statuses


@pytest.mark.asyncio
async def test_native_completion_before_dispatch_is_retained_without_delegation_row(tmp_path):
    source, adapter, runner, statuses = manager(tmp_path)
    ctx = TurnContext(source=source, session_key="route", session_id="parent", run_generation=1,
        tool_progress_enabled=True, progress_mode="all", _run_still_current=lambda: True)
    relay = TurnRunner(runner, ctx)
    tasks = []
    relay._schedule = lambda coro, *_: tasks.append(asyncio.create_task(coro))
    relay.progress_callback("subagent.complete", delegation_id="review-1", native_review=True, parent_task_id="a" * 32)
    await asyncio.gather(*tasks)
    assert await statuses.dispatch(source, "route", "parent", 1, "review-1")
    adapter.send.assert_awaited_once()
    assert statuses.items["review-1"]["state"] == "returned"
    assert not hasattr(runner, "_delegation_cards")


@pytest.mark.asyncio
async def test_restored_review_exact_completion_and_delete_retry(tmp_path):
    source, adapter, runner, statuses = manager(tmp_path)
    await statuses.dispatch(source, "route", "parent", 1, "review-1")
    restored = ReviewStatuses(runner, home=tmp_path)
    assert restored.items["review-1"]["state"] == "unknown"
    assert await restored.observe(source, "route", "parent", 1, "review-1", "subagent.complete")
    event = MessageEvent(source=source, text="result", internal=True,
                         metadata={"delegation_id": "review-1", "gateway_session_id": "parent"})
    adapter.delete_message.return_value = False
    await restored.delivered(restored.receipt(event, "route", 2))
    assert restored.items["review-1"]["retired"]
    adapter.delete_message.return_value = True
    again = ReviewStatuses(runner, home=tmp_path)
    await again.reconcile()
    assert again.items["review-1"]["message_id"] is None
    assert adapter.delete_message.await_count == 2
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_restored_review_receipt_matches_owner_without_replayed_live_event(tmp_path):
    source, adapter, runner, statuses = manager(tmp_path)
    await statuses.dispatch(source, "route", "parent", 1, "review-1")
    restored = ReviewStatuses(runner, home=tmp_path)
    event = MessageEvent(source=source, text="result", internal=True,
                         metadata={"delegation_id": "review-1", "gateway_session_id": "wrong"})
    assert not restored.receipt(event, "route", 2)
    event.metadata["gateway_session_id"] = "parent"
    await restored.delivered(restored.receipt(event, "route", 2))
    adapter.delete_message.assert_awaited_once()
