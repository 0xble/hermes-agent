"""Typing ownership across legacy adapters, approval buttons and delayed transport."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gateway.config import Platform, PlatformConfig
from gateway.platforms.typing_state import typing_metadata_for_event, typing_metadata_for_source
from gateway.platforms.typing_state import call_typing_with_metadata
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.test_slack import adapter as slack_fixture
from tests.gateway.test_telegram_approval_buttons import _make_adapter
import plugins.platforms.slack.adapter as slack_module

@pytest.mark.parametrize("args", [("C",), ("C", "working")])
@pytest.mark.parametrize("scoped", [False, True])
def test_metadata_forwarding_preserves_legacy_and_scoped_signatures(args, scoped):
    calls = []
    if scoped:
        def method(*values, metadata=None):
            calls.append((values, metadata))
    else:
        def method(*values):
            calls.append((values, None))
    metadata = {"thread_id": "T"}
    call_typing_with_metadata(method, *args, metadata=metadata)
    assert calls == [(args, metadata if scoped else None)]




@pytest.mark.parametrize("platform,chat_type,thread_id", [
    (Platform.SLACK, "dm", "171.1"),
    (Platform.TELEGRAM, "dm", "11"),
    (Platform.TELEGRAM, "group", "11"),
])
def test_normal_scope_metadata_agrees(platform, chat_type, thread_id):
    a = slack_fixture.__wrapped__() if platform == Platform.SLACK else TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda source: a
    source = SessionSource(platform=platform, chat_id="123", chat_type=chat_type, thread_id=thread_id)
    event = MessageEvent(text="test", source=source, message_id="171.1")
    initial = typing_metadata_for_event(event)
    _, _, status = runner._run_agent_progress_threading(source, event.message_id, False)
    assert a._typing_scope_key(source.chat_id, initial) == a._typing_scope_key(source.chat_id, status)

def test_slack_status_keeps_turn_scoped_recipient_identity():
    a = slack_fixture.__wrapped__()
    source = SessionSource(platform=Platform.SLACK, chat_id="C123", thread_id="171.1",
                           scope_id="T1", user_id="U1")
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda source: a
    _, _, status = runner._run_agent_progress_threading(source, "171.2", True)
    assert status["user_id"] == "U1"
    assert status["scope_id"] == status["slack_team_id"] == "T1"
    event = MessageEvent(text="test", source=source, message_id="171.2")
    assert a._typing_scope_key("C123", status) == a._typing_scope_key("C123", typing_metadata_for_event(event))


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_in_thread", [True, False])
async def test_top_level_slack_typing_scope_is_independent_of_send_routing(reply_in_thread):
    a = slack_fixture.__wrapped__()
    a.config.extra["dm_top_level_threads_as_sessions"] = False
    a.config.extra["reply_in_thread"] = reply_in_thread
    root_ts = "171.1"
    source_thread = a._session_thread_ts({"ts": root_ts}, root_ts, True, {})
    assert source_thread is None
    source = SessionSource(platform=Platform.SLACK, chat_id="D123", thread_id=source_thread)
    event = MessageEvent(text="test", source=source, message_id=root_ts)
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda source: a
    initial = typing_metadata_for_event(event)
    prompt_typing = typing_metadata_for_source(source, root_ts)
    assert initial == prompt_typing
    _, _, delivery = runner._run_agent_progress_threading(source, root_ts, False)
    assert (delivery or {}).get("thread_id") == (root_ts if reply_in_thread else None)
    await a.send_typing(source.chat_id, metadata=initial)
    if not reply_in_thread:
        a._app.client.assistant_threads_setStatus.assert_not_called()
    a.pause_typing_for_chat(source.chat_id, metadata=prompt_typing)
    assert a._typing_is_paused(source.chat_id, initial), (initial, prompt_typing, a._typing_paused)



@pytest.mark.asyncio
async def test_late_interim_reassert_cannot_restore_status_after_final(monkeypatch):
    """A status request spawned by an interim send is not owned/cancelled by refresh cleanup."""
    a = slack_fixture.__wrapped__()
    monkeypatch.setattr(slack_module, "_AGENT_SESSIONS_SUPPORTED", False)
    a._app.client.chat_postMessage = AsyncMock(return_value={"ts": "reply_ts"})
    md = {"thread_id": "171.1", "_interim_send": True}
    initial_set, reassert_started, release_reassert = (asyncio.Event() for _ in range(3))
    ui_status = [None]
    nonempty_calls = [0]

    async def set_status(**kwargs):
        value = kwargs["status"]
        if value:
            nonempty_calls[0] += 1
            if nonempty_calls[0] == 2:
                reassert_started.set()
                await release_reassert.wait()
        ui_status[0] = value
        if value and nonempty_calls[0] == 1:
            initial_set.set()

    a._app.client.assistant_threads_setStatus = AsyncMock(side_effect=set_status)
    owner = asyncio.create_task(a._keep_typing("D123", interval=10000, metadata=md))
    await asyncio.wait_for(initial_set.wait(), 2)
    interim = asyncio.create_task(a.send("D123", "working", metadata=md))
    try:
        await asyncio.wait_for(reassert_started.wait(), 2)
        # Final delivery owns exactly this surface and has completed all normal typing cleanup.
        await a._stop_typing_refresh("D123", owner, metadata=md)
        assert ui_status[0] == ""
        assert not a._typing_refresh_owners
        release_reassert.set()
        assert (await interim).success
        assert ui_status[0] == "", "the interim request restored a stuck idle status"
    finally:
        release_reassert.set()
        owner.cancel()
        interim.cancel()
        await asyncio.gather(owner, interim, return_exceptions=True)
