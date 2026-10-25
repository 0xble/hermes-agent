"""Prompt answers must resume the original refresh surface, not the answer's route."""
import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.platforms.typing_state import typing_metadata_for_event
from gateway.relay.adapter import RelayAdapter
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from tests.gateway.relay.test_relay_interactive import _adapter as relay_fixture


def _turn(platform, adapter):
    source = SessionSource(platform=platform, chat_id="D123", chat_type="dm", user_id="U1")
    event = MessageEvent(text="work", source=source, message_id="171.1")
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda source: adapter
    runner._session_key_for_source = lambda source: "session"
    _, _, send_metadata = runner._run_agent_progress_threading(source, event.message_id, False)
    ctx = TurnContext(source=source, session_key="session", event_message_id=event.message_id,
                      _status_adapter=adapter, _status_chat_id=source.chat_id,
                      _status_thread_metadata=send_metadata)
    turn = TurnRunner(runner, ctx)
    turn._close_native_stream_boundary = lambda *args, **kwargs: True
    turn._stream_consumer = lambda: None
    return runner, turn, event, typing_metadata_for_event(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.SLACK, Platform.MATTERMOST, "buzz"])
@pytest.mark.parametrize("answer", ["approve", "deny", "relay"])
async def test_approval_pause_answer_resumes_original_surface(monkeypatch, platform, answer):
    adapter, _ = relay_fixture(platform=str(getattr(platform, "value", platform)))
    adapter._platform_by_chat["D123"] = str(getattr(platform, "value", platform))
    adapter._send_lifecycle_ack = lambda *args: None
    runner, turn, event, refresh_metadata = _turn(platform, adapter)
    monkeypatch.setattr("gateway.run_turn_runner_approval_settle.register_timeout_notice", lambda *a, **k: None)
    monkeypatch.setattr("tools.approval.resolve_gateway_approval", lambda *a, **k: 1)

    def schedule(coro, reason):
        coro.close()
        future = Future()
        future.set_result(SimpleNamespace(success=True, message_id="prompt"))
        return future

    turn._schedule = schedule
    sibling = {"thread_id": "sibling"}
    adapter.pause_typing_for_chat("D123", metadata=sibling)
    turn._approval_notify_sync({"command": "pwd"})
    assert adapter._typing_is_paused("D123", refresh_metadata), "prompt did not pause the refresh transport"
    reply = MessageEvent(text=f"/{answer}", source=event.source, message_id="172.2")
    if answer == "relay":
        await adapter._resolve_exec_approval({"session_key": "session"}, "once", "D123", {})
    else:
        await runner._deliver_approval_confirmation(reply, "resolved", answer)
    assert not adapter._typing_is_paused("D123", refresh_metadata), "answer resumed its own route instead of the paused turn"
    assert adapter._typing_is_paused("D123", sibling), "answer resumed a sibling thread"
    # Final cleanup, even without a running refresh task, must retire prompt ownership.
    await adapter._stop_typing_refresh("D123", metadata=refresh_metadata)
    assert not getattr(adapter, "_typing_prompt_surfaces", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.SLACK, Platform.MATTERMOST, "buzz"])
async def test_clarify_pause_typed_answer_resumes_original_surface(monkeypatch, platform):
    from tools import clarify_gateway
    adapter, _ = relay_fixture(platform=str(getattr(platform, "value", platform)))
    runner, turn, event, refresh_metadata = _turn(platform, adapter)
    runner._pending_event_audio_paths = lambda event: []
    runner._prepare_clarify_reply_text = AsyncMock(return_value="the answer")
    adapter.send_clarify = AsyncMock(return_value=SimpleNamespace(success=True))
    adapter.retire_clarify_card = AsyncMock()
    loop = asyncio.get_running_loop()
    turn._schedule = lambda coro, reason: asyncio.run_coroutine_threadsafe(coro, loop)
    sibling = {"thread_id": "sibling"}
    adapter.pause_typing_for_chat("D123", metadata=sibling)

    async def answer():
        assert adapter._typing_is_paused("D123", refresh_metadata), "clarify did not pause the refresh transport"
        reply = MessageEvent(text="the answer", source=event.source, message_id="172.2")
        assert await runner._hm_clarify_reply(reply, reply.source, "session") == ""
        assert not adapter._typing_is_paused("D123", refresh_metadata), "typed answer missed the paused surface"
        assert adapter._typing_is_paused("D123", sibling)

    def wait(future, **kwargs):
        future.result(timeout=2)
        try:
            asyncio.run_coroutine_threadsafe(answer(), loop).result(timeout=2)
            return "the answer", True
        finally:
            clarify_gateway.clear_session("session")

    monkeypatch.setattr("gateway.run_turn_runner_clarify_delivery._clarify_send_then_wait", wait)
    assert await asyncio.to_thread(turn._ask_clarify_question, "Question?", None, False) == ("the answer", True)
    await adapter._stop_typing_refresh("D123", metadata=refresh_metadata)
    assert not getattr(adapter, "_typing_prompt_surfaces", {})
