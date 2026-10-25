"""Refresh ownership must not overwrite adapter transport state or send routing."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import _thread_metadata_for_event
from gateway.platforms.event import MessageEvent
from gateway.platforms.signal import SignalAdapter
from gateway.relay.adapter import RelayAdapter
from gateway.session import SessionSource
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.gateway.relay.test_relay_adapter import _CaptureTransport, make_desc
from tests.gateway.test_slack import adapter as slack_fixture


def _refresh_gate(monkeypatch):
    """Advance real base refreshes explicitly, without changing adapter sends."""
    from gateway.platforms import base

    reached, ticks = asyncio.Event(), asyncio.Queue()
    sleep = asyncio.sleep

    async def gated_sleep(delay):
        task = asyncio.current_task()
        if task is not None and task.get_name() == "base-refresh" and delay:
            reached.set()
            await ticks.get()
        else:
            await sleep(delay)

    monkeypatch.setattr(base.asyncio, "sleep", gated_sleep)
    return reached, ticks


@pytest.mark.asyncio
async def test_discord_base_refresh_starts_real_transport_loop(monkeypatch):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    posted = asyncio.Event()

    async def request(route):
        assert route.method == "POST"
        assert route.path == "/channels/{channel_id}/typing"
        posted.set()

    adapter._client = SimpleNamespace(http=SimpleNamespace(request=AsyncMock(side_effect=request)))
    reached, _ = _refresh_gate(monkeypatch)
    owner = asyncio.create_task(adapter._keep_typing("123"), name="base-refresh")
    try:
        await asyncio.wait_for(reached.wait(), 2)
        assert posted.is_set(), "base owner suppressed Discord's first typing POST"
        assert adapter._typing_tasks["123"] is not owner
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_signal_interim_send_does_not_cancel_base_refresh(monkeypatch):
    adapter = SignalAdapter(PlatformConfig(enabled=True, extra={"account": "+15550001111"}))
    adapter._rpc = AsyncMock(return_value={"timestamp": 123})
    reached, ticks = _refresh_gate(monkeypatch)
    owner = asyncio.create_task(adapter._keep_typing("+15550002222"), name="base-refresh")
    try:
        await asyncio.wait_for(reached.wait(), 2)
        result = await adapter.send("+15550002222", "working", metadata={"_interim_send": True})
        assert result.success
        assert not owner.done(), "Signal's stop RPC cancelled the base refresh owner"
        reached.clear()
        ticks.put_nowait(None)
        await asyncio.wait_for(reached.wait(), 2)
        assert sum(c.args[0] == "sendTyping" and not c.args[1].get("stop")
                   for c in adapter._rpc.await_args_list) == 2
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_relay_thread_phrase_reaches_transport_frame():
    transport = _CaptureTransport()
    adapter = RelayAdapter(PlatformConfig(), make_desc(platform="slack"), transport=transport)
    adapter._platform_by_chat["D123"] = "slack"
    adapter.set_status_text("D123", "reading A", metadata={"thread_id": "171.1"})
    adapter.set_status_text("D123", "reading B", metadata={"thread_id": "171.2"})
    for thread, phrase in [("171.1", "reading A"), ("171.2", "reading B")]:
        await adapter.send_typing("D123", metadata={"thread_id": thread})
        assert transport.sent == {
            "op": "typing", "chat_id": "D123", "content": phrase,
            "metadata": {"thread_id": thread},
        }
        assert transport.sent_platform == "slack"


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", [None, "171.0"])
async def test_slack_flat_media_and_error_keep_send_routing(tmp_path, monkeypatch, thread_id):
    adapter = slack_fixture.__wrapped__()
    monkeypatch.setattr("plugins.platforms.slack.adapter._AGENT_SESSIONS_SUPPORTED", False)
    adapter.config.extra.update(reply_in_thread=False, dm_top_level_threads_as_sessions=False)
    adapter._app.client.files_upload_v2 = AsyncMock(return_value={"ok": True})
    adapter._app.client.chat_postMessage = AsyncMock(return_value={"ts": "172.1"})
    source = SessionSource(platform=Platform.SLACK, chat_id="D123", chat_type="dm", thread_id=thread_id)
    event = MessageEvent(text="test", source=source, message_id="171.1")
    metadata = _thread_metadata_for_event(event)
    path = tmp_path / "document.txt"
    path.write_text("test attachment")
    result = await adapter.send_document("D123", str(path), metadata=metadata)
    assert result.success
    assert adapter._app.client.files_upload_v2.call_args.kwargs["thread_ts"] == thread_id
    await adapter._notify_turn_error(event, RuntimeError("transport failed"))
    assert adapter._app.client.chat_postMessage.call_args.kwargs.get("thread_ts") == thread_id
