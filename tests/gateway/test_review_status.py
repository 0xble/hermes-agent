import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, SendResult
from gateway.review_status import ReviewStatuses, render
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


def _source(thread_id="8"):
    return SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id=thread_id)


def _adapter():
    return SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="review-status")),
        edit_message=AsyncMock(return_value=SendResult(success=True, message_id="review-status")),
        delete_message=AsyncMock(return_value=True),
    )


def test_native_review_render_is_an_ordinary_bold_line_not_a_quote_card():
    assert render("dispatched") == "⚖ **Review dispatched**"
    assert render("reviewing", 0, now=125) == "⚖ **Reviewing · 2 min**"
    assert render("returned") == "⚖ **Review returned**"
    assert all(not render(state).startswith(">") for state in ("dispatched", "returned", "unknown"))


@pytest.mark.asyncio
async def test_native_review_status_lifecycle_uses_exact_enum_adapter_and_parent_receipt(tmp_path):
    source, adapter = _source(), _adapter()

    class Runner:
        def __init__(self):
            self.adapters = {Platform.TELEGRAM: adapter}
            self._primary_profile_name = "default"

        def _adapter_for_source(self, resolved_source):
            assert resolved_source.platform is Platform.TELEGRAM
            return self.adapters[resolved_source.platform]

        @staticmethod
        def _thread_metadata_for_source(resolved_source):
            return {"thread_id": resolved_source.thread_id}

    runner = Runner()
    statuses = ReviewStatuses(runner, home=tmp_path)
    assert await statuses.dispatch(source, "route", "parent", 1, "review-1")
    assert adapter.send.call_args.args == ("42", "⚖ **Review dispatched**")
    assert adapter.send.call_args.kwargs == {"metadata": {"thread_id": "8", "hermes_status": True}}

    assert await statuses.observe(source, "route", "parent", 1, "review-1", "subagent.start")
    assert "⚖ **Reviewing · 0 min**" == adapter.edit_message.call_args.args[2]
    assert await statuses.observe(source, "route", "parent", 1, "review-1", "subagent.complete")
    assert adapter.edit_message.call_args.args[2] == "⚖ **Review returned**"

    event = MessageEvent(text="review result", source=source, internal=True, metadata={
        "delegation_id": "review-1", "gateway_session_id": "parent",
    })
    receipt = statuses.receipt(event, "route", 2)
    assert receipt == {"review-1": {"generation": 1, "owner": {
        "session_key": "route", "session_id": "parent", "chat_id": "42", "thread_id": "8",
    }}}
    # A failed parent final must leave the returned status visible.
    assert adapter.delete_message.await_count == 0
    await statuses.delivered({})
    assert adapter.delete_message.await_count == 0
    # The caller invokes this only after its real final transport reports success.
    await statuses.delivered(receipt)
    adapter.delete_message.assert_awaited_once_with("42", "review-status")
    assert statuses.items["review-1"]["retired"] is True


@pytest.mark.asyncio
async def test_native_review_ownership_failure_and_restart_never_resend_ambiguous_status(tmp_path):
    source, adapter = _source(), _adapter()
    runner = SimpleNamespace(
        _adapter_for_source=lambda resolved_source: adapter,
        _thread_metadata_for_source=lambda resolved_source: {"thread_id": resolved_source.thread_id},
    )
    statuses = ReviewStatuses(runner, home=tmp_path)
    adapter.send.return_value = SendResult(success=False, error="timeout")
    assert not await statuses.dispatch(source, "route", "parent", 1, "review-1")
    assert not await statuses.dispatch(source, "route", "parent", 1, "review-1")
    assert adapter.send.await_count == 1
    assert not statuses.owns(_source("other"), "route", "parent", 1, "review-1")

    # A process restart preserves the uncertain send rather than creating a duplicate.
    restored = ReviewStatuses(runner, home=tmp_path)
    assert restored.items["review-1"]["state"] == "unknown"
    assert not await restored.dispatch(source, "route", "parent", 1, "review-1")
    assert adapter.send.await_count == 1


@pytest.mark.asyncio
async def test_turn_runner_routes_native_review_lifecycle_without_generic_card(tmp_path):
    source, adapter = _source(), _adapter()
    runner = SimpleNamespace(
        _adapter_for_source=lambda resolved_source: adapter,
        _thread_metadata_for_source=lambda resolved_source: {"thread_id": resolved_source.thread_id},
    )
    ctx = TurnContext(source=source, session_key="route", session_id="parent", run_generation=1,
        tool_progress_enabled=True, progress_mode="all", _run_still_current=lambda: True)
    relay = TurnRunner(runner, ctx)
    tasks = []
    relay._schedule = lambda coro, *_: tasks.append(asyncio.create_task(coro))
    statuses = ReviewStatuses(runner, home=tmp_path)
    runner._review_statuses = statuses
    assert await statuses.dispatch(source, "route", "parent", 1, "review-1")

    relay.progress_callback("subagent.start", delegation_id="review-1", parent_task_id="not-a-card")
    await asyncio.gather(*tasks)
    assert adapter.edit_message.call_args.args[2] == "⚖ **Reviewing · 0 min**"
    assert not hasattr(runner, "_delegation_cards")
    tasks.clear()
    relay.progress_callback("subagent.complete", delegation_id="review-1", parent_task_id="not-a-card", status="completed")
    await asyncio.gather(*tasks)
    assert adapter.edit_message.call_args.args[2] == "⚖ **Review returned**"
