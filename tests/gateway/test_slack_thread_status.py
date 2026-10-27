"""Slack assistant status stays continuous per thread across siblings and interim deliveries."""

import asyncio
from unittest.mock import AsyncMock, call

import pytest

# test_slack's autouse pins (legacy status API, temporary document cache) apply here too.
from tests.gateway.test_slack import (  # noqa: F401 - pytest fixtures
    _pin_legacy_assistant_threads_api, _redirect_cache,
)
from tests.gateway.test_slack import adapter as slack_fixture


@pytest.fixture()
def adapter():
    return slack_fixture.__wrapped__()


@pytest.mark.asyncio
async def test_thread_typing_continuity_during_sibling_pause_and_completion(adapter, monkeypatch):
    """A waiting/finalizing DM sibling must not skip ticks or erase the live phrase."""
    from gateway.platforms import base as base_module

    gates = {name: asyncio.Queue() for name in ("thread_a", "thread_b")}
    waiting = {name: asyncio.Event() for name in gates}

    real_sleep = asyncio.sleep

    async def tick_gate(delay):
        if delay == 0:
            return await real_sleep(0)
        task = asyncio.current_task()
        assert task is not None
        name = task.get_name()
        waiting[name].set()
        await gates[name].get()

    monkeypatch.setattr(base_module.asyncio, "sleep", tick_gate)
    status = adapter._app.client.assistant_threads_setStatus = AsyncMock()
    a, b = {"thread_id": "thread_a"}, {"thread_id": "thread_b"}
    adapter.set_status_text("D123", "reading A", metadata=a)
    adapter.set_status_text("D123", "reading B", metadata=b)
    tasks = [asyncio.create_task(adapter._keep_typing("D123", metadata=md), name=md["thread_id"])
             for md in (a, b)]
    try:
        for event in waiting.values():
            await asyncio.wait_for(event.wait(), 5)
        adapter.pause_typing_for_chat("D123", metadata=a)
        before = status.await_count
        waiting["thread_b"].clear()
        gates["thread_b"].put_nowait(None)
        await asyncio.wait_for(waiting["thread_b"].wait(), 5)
        assert status.await_count == before + 1
        assert status.call_args == call(channel_id="D123", thread_ts="thread_b", status="reading B")
        await adapter._stop_typing_refresh("D123", tasks[0], metadata=a)
        before = status.await_count
        waiting["thread_b"].clear()
        gates["thread_b"].put_nowait(None)
        await asyncio.wait_for(waiting["thread_b"].wait(), 5)
        assert status.await_count == before + 1
        assert status.call_args == call(channel_id="D123", thread_ts="thread_b", status="reading B")
        assert not any(c.kwargs["thread_ts"] == "thread_b" and not c.kwargs["status"]
                       for c in status.call_args_list)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["commentary", "progress", "draft", "task_card"])
async def test_intermediate_delivery_restores_running_thread_status(adapter, delivery):
    """Slack post auto-clear must be repaired before the delivery call returns."""
    md = {"thread_id": "parent_ts", "_interim_send": True}
    status = adapter._app.client.assistant_threads_setStatus = AsyncMock()
    adapter._app.client.chat_postMessage = AsyncMock(return_value={"ts": "reply_ts"})
    adapter._app.client.chat_startStream = AsyncMock(return_value={"ts": "draft_ts"})
    adapter._app.client.api_call = AsyncMock(return_value={"ts": "card_ts"})
    adapter.set_status_text("D123", "gathering information", metadata=md)
    # Hold an actual active owner, without waiting on wall-clock refresh intervals.
    owner = asyncio.create_task(asyncio.Event().wait())
    adapter.__dict__.setdefault("_typing_refresh_owners", {})[adapter._typing_scope_key("D123", md)] = owner
    try:
        await adapter.send_typing("D123", metadata=md)
        before = status.await_count
        if delivery == "progress":
            result = await adapter.send_or_update_status("D123", "progress", "tool running", metadata=md)
        elif delivery == "draft":
            result = await adapter.send_draft("D123", 1, "working", metadata=md)
        elif delivery == "task_card":
            result = await adapter.send_native_task_card_progress(
                "D123", [{"id": "tool", "title": "working"}], metadata=md)
        else:
            result = await adapter.send("D123", "working", metadata=md)
        assert result.success
        assert status.await_count == before + 1
        assert status.call_args == call(channel_id="D123", thread_ts="parent_ts", status="gathering information")
        assert not any(not c.kwargs["status"] for c in status.call_args_list)
        # True turn end still clears immediately, even if its refresh owner has not exited yet.
        await adapter.send("D123", "done", metadata={"thread_id": "parent_ts", "notify": True})
        assert status.call_args.kwargs["status"] == ""
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
