"""Native progress cards are transient, unlike the actual assistant reply."""

import asyncio
import json

from hermes_constants import get_hermes_home
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from slack_sdk.errors import SlackApiError

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter
from plugins.platforms.slack import adapter as slack_module, task_cards


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
@pytest.mark.parametrize("include_team", [True, False])
async def test_turn_cleanup_stops_then_deletes_only_its_card(slack, include_team):
    adapter, client = slack
    adapter._team_clients["T1"] = client
    metadata = METADATA if include_team else {"thread_id": "thread-1"}
    result = await adapter.send_native_task_card_progress("C1", TASKS, metadata=metadata)
    assert result.success
    ledger = get_hermes_home() / "state" / "slack_task_cards.json"
    entries = json.loads(ledger.read_text())
    assert [(entry["team_id"], entry["channel"], entry["ts"]) for entry in entries] == [("T1", "C1", "card-1")]
    assert entries[0]["created_at"] > 0
    client.api_call.reset_mock()

    await asyncio.gather(*(
        adapter.stop_native_task_card_progress("C1", metadata=metadata) for _ in range(2)
    ))

    assert [(call.args[0], call.kwargs["json"]) for call in client.api_call.await_args_list] == [
        ("chat.stopStream", {"channel": "C1", "ts": "card-1"}),
        ("chat.delete", {"channel": "C1", "ts": "card-1"}),
    ]
    assert not adapter._native_task_card_streams
    assert json.loads(ledger.read_text()) == []
    # The thread can be reused without inheriting stopped state.
    result = await adapter.send_native_task_card_progress("C1", TASKS, metadata=metadata)
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
    entries = json.loads((get_hermes_home() / "state" / "slack_task_cards.json").read_text())
    assert [entry["ts"] for entry in entries] == ([] if error == "message_not_found" else ["card-1"])
    await adapter._task_card_cleanup.close()
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
    ledger = get_hermes_home() / "state" / "slack_task_cards.json"
    await adapter.send_native_task_card_progress("C1", TASKS, metadata=METADATA)
    await adapter.stop_native_task_card_progress("C1", metadata=METADATA)
    assert client.api_call.await_args.args[0] == "chat.stopStream"
    assert not any(call.args[0] == "chat.delete" for call in client.api_call.await_args_list)
    assert not adapter._native_task_card_streams
    await adapter._task_card_cleanup.sweep()
    adapter._task_card_cleanup.start(immediate=True)
    assert not ledger.exists()
    assert adapter._task_card_cleanup.task is None


@pytest.mark.asyncio
async def test_failed_delete_is_retried_until_confirmed_even_after_24h(slack, monkeypatch):
    adapter, client = slack
    adapter._team_clients["T1"] = client
    monkeypatch.setattr(task_cards, "RETRY_SECONDS", 0)
    await adapter.send_native_task_card_progress("C1", TASKS, metadata=METADATA)
    attempts = 0
    original = client.api_call.side_effect

    async def api_call(method, *, json):
        nonlocal attempts
        if method == "chat.delete":
            attempts += 1
            if attempts < 3:
                raise SlackApiError("still streaming", {"error": "cant_delete_message"})
        return await original(method, json=json)

    client.api_call.side_effect = api_call
    await adapter.stop_native_task_card_progress("C1", metadata=METADATA)
    ledger = adapter._task_card_cleanup.path
    entries = json.loads(ledger.read_text())
    assert [entry["ts"] for entry in entries] == ["card-1"]
    entries[0]["created_at"] = 0
    ledger.write_text(json.dumps(entries))
    assert adapter._task_card_cleanup.task is not None
    try:
        await asyncio.wait_for(adapter._task_card_cleanup.task, timeout=5)
        assert attempts == 3
        assert json.loads(ledger.read_text()) == []
    finally:
        await adapter._task_card_cleanup.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ephemeral", [True, False])
async def test_connect_sweeps_only_ledger_cards_and_preserves_live_streams(
    slack, monkeypatch, ephemeral,
):
    previous, client = slack
    await previous.send_native_task_card_progress("C1", TASKS, metadata=METADATA)
    ledger = previous._task_card_cleanup.path
    # A fresh adapter has no memory of the first process's in-flight stream.
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake", extra={
        "native_task_cards": True, "native_task_cards_ephemeral": ephemeral}))
    adapter._app = SimpleNamespace(client=client)
    adapter._team_clients["T1"] = client
    await adapter.send_native_task_card_progress(
        "C1", TASKS, metadata={**METADATA, "thread_id": "live-thread"})
    before = ledger.read_text()
    client.api_call.reset_mock()
    swept = asyncio.Event()
    sweep = adapter._task_card_cleanup.sweep

    async def observed_sweep():
        await sweep()
        swept.set()

    monkeypatch.setattr(adapter._task_card_cleanup, "sweep", observed_sweep)

    async def api_call(method, *, json):
        if method == "chat.delete":
            assert json["ts"] == "card-1"
            raise SlackApiError("already gone", {"error": "message_not_found"})
        return {"ok": True}

    async def authenticate(token, proxy):
        adapter._team_clients["T1"] = client

    client.api_call.side_effect = api_call
    monkeypatch.setattr(slack_module, "SLACK_AVAILABLE", True)
    monkeypatch.setattr(slack_module, "_get_scoped_secret", lambda _: "xapp-fake")
    monkeypatch.setattr(slack_module, "_resolve_slack_proxy_url", lambda: None)
    monkeypatch.setattr(slack_module, "AsyncApp", lambda **_: SimpleNamespace(client=client))
    monkeypatch.setattr(adapter, "_new_web_client", lambda *args: client)
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *args: True)
    monkeypatch.setattr(adapter, "_release_platform_lock", Mock())
    monkeypatch.setattr(adapter, "_authenticate_workspace", authenticate)
    monkeypatch.setattr(adapter, "_register_bolt_handlers", Mock())
    monkeypatch.setattr(adapter, "_start_socket_mode_handler", Mock())
    monkeypatch.setattr(adapter, "_ensure_socket_watchdog", Mock())
    try:
        assert await adapter.connect()
        if ephemeral:
            await asyncio.wait_for(swept.wait(), timeout=5)
            assert [entry["ts"] for entry in json.loads(ledger.read_text())] == ["card-2"]
            assert [(call.args[0], call.kwargs["json"]["ts"])
                    for call in client.api_call.await_args_list] == [
                        ("chat.stopStream", "card-1"), ("chat.delete", "card-1")]
        else:
            assert ledger.read_text() == before
            assert adapter._task_card_cleanup.task is None
            client.api_call.assert_not_awaited()
    finally:
        await adapter._task_card_cleanup.close()
