"""A retired Slack refresh owner cannot clear its successor's status or prompt."""

import asyncio

import pytest

from gateway.platforms.typing_state import pause_typing_for_prompt
from tests.gateway.test_slack import adapter as slack_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["cancel", "stop", "late_stop"])
async def test_retired_slack_owner_preserves_successor_surface(cleanup):
    adapter = slack_fixture.__wrapped__()
    metadata = {"thread_id": "171.1"}
    client = adapter._app.client
    first_status, second_status = asyncio.Event(), asyncio.Event()
    statuses = []

    async def set_status(**kwargs):
        statuses.append(kwargs["status"])
        if len(statuses) == 1:
            first_status.set()
        elif len(statuses) == 2:
            second_status.set()

    client.assistant_threads_setStatus.side_effect = set_status
    old = asyncio.create_task(adapter._keep_typing("D123", interval=10000, metadata=metadata))
    new = None
    try:
        await asyncio.wait_for(first_status.wait(), 2)
        if cleanup == "late_stop":
            await adapter._stop_typing_refresh("D123", old, metadata=metadata)
            statuses.clear()
        new = asyncio.create_task(adapter._keep_typing("D123", interval=10000, metadata=metadata))
        if cleanup == "late_stop":
            first_status.clear()
            await asyncio.wait_for(first_status.wait(), 2)
        else:
            await asyncio.wait_for(second_status.wait(), 2)
        adapter.set_status_text("D123", "Waiting for approval", metadata=metadata)
        pause_typing_for_prompt(adapter, "session", "D123", metadata)
        if cleanup == "cancel":
            old.cancel()
            await asyncio.wait_for(old, 2)
        else:
            await adapter._stop_typing_refresh("D123", old, metadata=metadata)
        scope = adapter._typing_scope_key("D123", metadata)
        assert adapter._typing_refresh_owners[scope] is new
        assert not new.done()
        assert statuses[-1], "a retired owner cleared the live successor's status"
        assert adapter._typing_is_paused("D123", metadata)
        assert adapter.get_status_text("D123", metadata) == "Waiting for approval"
        assert "session" in adapter._typing_prompt_surfaces
    finally:
        old.cancel()
        if new is not None:
            new.cancel()
        await asyncio.gather(old, *([new] if new is not None else []), return_exceptions=True)
    assert statuses[-1] == ""
    assert not adapter._typing_refresh_owners
    assert not adapter._typing_is_paused("D123", metadata)
    assert adapter.get_status_text("D123", metadata) is None
    assert not adapter._typing_prompt_surfaces
