"""Native progress cards are transient, unlike the actual assistant reply."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from slack_sdk.errors import SlackApiError

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


@pytest.fixture
def slack():
    adapter = SlackAdapter(PlatformConfig(enabled=True, extra={"native_task_cards": True}))
    client = AsyncMock()
    adapter._app = SimpleNamespace(client=client)
    counter = 0

    async def api_call(method, *, json):
        nonlocal counter
        if method == "chat.startStream":
            counter += 1
            return {"ok": True, "ts": f"card-{counter}"}
        return {"ok": True}

    client.api_call.side_effect = api_call
    return adapter, client


TASKS = [{"id": "call-1", "title": "terminal", "status": "in_progress"}]
METADATA = {"thread_id": "thread-1", "slack_team_id": "T1"}


@pytest.mark.asyncio
async def test_turn_cleanup_stops_then_deletes_only_its_card(slack):
    adapter, client = slack
    adapter._team_clients["T1"] = client
    result = await adapter.send_native_task_card_progress("C1", TASKS, metadata=METADATA)
    assert result.success
    client.api_call.reset_mock()

    await asyncio.gather(*(
        adapter.stop_native_task_card_progress("C1", metadata=METADATA) for _ in range(2)
    ))

    assert [(call.args[0], call.kwargs["json"]) for call in client.api_call.await_args_list] == [
        ("chat.stopStream", {"channel": "C1", "ts": "card-1"}),
        ("chat.delete", {"channel": "C1", "ts": "card-1"}),
    ]
    assert not adapter._native_task_card_streams
    # The thread can be reused without inheriting stopped state.
    result = await adapter.send_native_task_card_progress("C1", TASKS, metadata=METADATA)
    assert result.success and result.message_id == "card-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_error", ["message_not_in_streaming_state", "internal_error"])
async def test_stop_failure_does_not_prevent_deletion(slack, stop_error):
    adapter, client = slack
    await adapter.send_native_task_card_progress("C1", TASKS, metadata=METADATA)
    original = client.api_call.side_effect

    async def api_call(method, *, json):
        if method == "chat.stopStream":
            raise SlackApiError("stop failed", {"ok": False, "error": stop_error})
        return await original(method, json=json)

    client.api_call.side_effect = api_call
    await adapter.stop_native_task_card_progress("C1", metadata=METADATA)
    assert client.api_call.await_args.args[0] == "chat.delete"
    assert not adapter._native_task_card_streams


@pytest.mark.asyncio
async def test_expired_cards_are_also_deleted_at_turn_end(slack):
    adapter, client = slack
    await adapter.send_native_task_card_progress("C1", TASKS, metadata=METADATA)
    original = client.api_call.side_effect

    async def api_call(method, *, json):
        if method == "chat.appendStream" and json["ts"] == "card-1":
            raise SlackApiError("expired", {"ok": False, "error": "message_not_in_streaming_state"})
        return await original(method, json=json)

    client.api_call.side_effect = api_call
    assert (await adapter.send_native_task_card_progress("C1", TASKS, metadata=METADATA)).success
    await adapter.stop_native_task_card_progress("C1", metadata=METADATA)
    assert [call.kwargs["json"]["ts"] for call in client.api_call.await_args_list
            if call.args[0] == "chat.delete"] == ["card-1", "card-2"]
    assert not adapter._native_task_card_streams


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["message_not_found", "cant_delete_message"])
async def test_delete_failure_clears_turn_state_and_does_not_mask_reply(slack, error, caplog):
    adapter, client = slack
    await adapter.send_native_task_card_progress("C1", TASKS, metadata=METADATA)
    original = client.api_call.side_effect

    async def api_call(method, *, json):
        if method == "chat.delete":
            raise SlackApiError("delete failed", {"ok": False, "error": error})
        return await original(method, json=json)

    client.api_call.side_effect = api_call
    await adapter.stop_native_task_card_progress("C1", metadata=METADATA)
    assert not adapter._native_task_card_streams
    if error != "message_not_found":
        assert "cant_delete_message" in caplog.text


@pytest.mark.asyncio
async def test_failed_reopen_still_removes_the_expired_card(slack):
    adapter, client = slack
    await adapter.send_native_task_card_progress("C1", TASKS, metadata=METADATA)
    original = client.api_call.side_effect

    async def api_call(method, *, json):
        if method == "chat.appendStream":
            raise SlackApiError("expired", {"ok": False, "error": "message_not_in_streaming_state"})
        if method == "chat.startStream":
            raise ConnectionError("reopen failed")
        return await original(method, json=json)

    client.api_call.side_effect = api_call
    result = await adapter.send_native_task_card_progress("C1", TASKS, metadata=METADATA)
    assert not result.success
    client.api_call.reset_mock()
    await adapter.stop_native_task_card_progress("C1", metadata=METADATA)
    assert [(call.args[0], call.kwargs["json"]) for call in client.api_call.await_args_list] == [
        ("chat.delete", {"channel": "C1", "ts": "card-1"}),
    ]
    assert not adapter._native_task_card_streams


@pytest.mark.asyncio
async def test_retention_opt_out_preserves_completed_card(slack):
    adapter, client = slack
    adapter.config.extra["native_task_cards_ephemeral"] = False
    await adapter.send_native_task_card_progress("C1", TASKS, metadata=METADATA)
    await adapter.stop_native_task_card_progress("C1", metadata=METADATA)
    assert client.api_call.await_args.args[0] == "chat.stopStream"
    assert not any(call.args[0] == "chat.delete" for call in client.api_call.await_args_list)
    assert not adapter._native_task_card_streams
