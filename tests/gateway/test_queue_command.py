"""Tests for the gateway /queue command handler (running-agent path).

/queue stores a turn-boundary follow-up in the adapter's pending queue
without interrupting the active run. The queued event must carry the
full payload — media attachments and reply context — not just the text.
Previously the handler rebuilt the event with only text/type/source/
message_id/channel_prompt, silently dropping any photo/document/reply
metadata the user attached to the /queue message.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_runner(session_entry: SessionEntry):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._pending_approvals = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner, adapter


def _session_entry() -> SessionEntry:
    return SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )


def _running(runner):
    """Mark the session as having a running agent so /queue hits the
    early-intercept path."""
    sk = build_session_key(_make_source())
    runner._running_agents[sk] = MagicMock()
    return sk


@pytest.mark.asyncio
async def test_queue_preserves_photo_media():
    """A /queue carrying a photo must keep the attachment + type."""
    runner, adapter = _make_runner(_session_entry())
    sk = _running(runner)

    event = MessageEvent(
        text="/queue look at this",
        message_type=MessageType.PHOTO,
        source=_make_source(),
        message_id="q-photo",
        media_urls=["/tmp/photo-a.jpg"],
        media_types=["image/jpeg"],
    )
    result = await runner._handle_message(event)

    assert result is not None and "queued" in result.lower()
    queued = adapter._pending_messages[sk]
    assert queued.text == "look at this"
    assert queued.message_type == MessageType.PHOTO
    assert queued.media_urls == ["/tmp/photo-a.jpg"]
    assert queued.media_types == ["image/jpeg"]


@pytest.mark.asyncio
async def test_queue_preserves_reply_context():
    runner, adapter = _make_runner(_session_entry())
    sk = _running(runner)

    event = MessageEvent(
        text="/queue and this",
        source=_make_source(),
        message_id="q-reply",
        reply_to_message_id="orig-7",
        reply_to_text="the original message",
        reply_to_author_id="a1",
        reply_to_author_name="alice",
    )
    result = await runner._handle_message(event)

    assert result is not None and "queued" in result.lower()
    queued = adapter._pending_messages[sk]
    assert queued.reply_to_message_id == "orig-7"
    assert queued.reply_to_text == "the original message"
    assert queued.reply_to_author_id == "a1"
    assert queued.reply_to_author_name == "alice"


@pytest.mark.asyncio
@pytest.mark.parametrize("starting", [False, True])
async def test_busy_moa_preserves_command_and_context_without_switching(starting):
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner, adapter = _make_runner(_session_entry())
    sk = _running(runner)
    if starting:
        runner._running_agents[sk] = _AGENT_PENDING_SENTINEL
    active = runner._running_agents[sk]
    prior = {"provider": "openrouter", "model": "normal"}
    runner._session_state(sk).conversation.model_override = prior
    runner._evict_cached_agent = MagicMock()
    event = MessageEvent(
        text="/moa@mybot review this", source=_make_source(), message_id="moa-1",
        message_type=MessageType.PHOTO, media_urls=["/tmp/photo.jpg"],
        media_types=["image/jpeg"], reply_to_message_id="quoted",
        reply_to_text="quoted proposal", reply_to_author_name="tester",
        reply_to_is_own_message=True, channel_context="channel context",
        metadata={"owner": True}, turn_reasoning_config={"effort": "high"},
    )
    reply = await runner._handle_message(event)
    assert reply is not None and "queued" in reply.lower() and "moa" in reply.lower()
    queued = adapter._pending_messages[sk]
    assert queued.text == event.text
    assert queued.reply_to_text == "quoted proposal"
    assert queued.media_urls == ["/tmp/photo.jpg"]
    assert queued.source == event.source
    assert queued.message_id == "moa-1"
    assert queued.metadata == event.metadata
    assert queued.turn_reasoning_config == event.turn_reasoning_config
    assert not getattr(queued, "_moa_disable_after_turn", False)
    assert runner._session_state(sk).conversation.model_override is prior
    assert runner._running_agents[sk] is active
    runner._evict_cached_agent.assert_not_called()
    if not starting:
        active.interrupt.assert_not_called()
        active.steer.assert_not_called()


@pytest.mark.asyncio
async def test_busy_moa_fifo_then_idle_dispatch_restores_each_turn(monkeypatch):
    runner, adapter = _make_runner(_session_entry())
    sk = _running(runner)
    prior = {"provider": "openrouter", "model": "normal"}
    runner._session_state(sk).conversation.model_override = prior
    runner._evict_cached_agent = MagicMock()
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    for text in ("/queue first", "/moa second", "/moa third", "/queue fourth"):
        reply = await runner._handle_message(MessageEvent(text=text, source=_make_source()))
        assert reply is not None and "queued" in reply.lower()
    assert runner._queue_depth(sk, adapter=adapter) == 4
    del runner._running_agents[sk]
    texts = []
    while sk in adapter._pending_messages:
        event = adapter._pending_messages.pop(sk)
        runner._promote_queued_event(sk, adapter, event)
        texts.append(event.text)
        if event.get_command() == "moa":
            handled, reply = await runner._hm_dispatch_canonical_command(
                event, event.source, sk, "moa")
            assert (handled, reply) == (False, None)
            assert runner._session_state(sk).conversation.model_override["provider"] == "moa"
            assert not event.text.startswith("/moa")
            runner._restore_moa_one_shot(event, sk)
            assert runner._session_state(sk).conversation.model_override is prior
    assert texts == ["first", "/moa second", "/moa third", "fourth"]
    assert runner._queue_depth(sk, adapter=adapter) == 0


@pytest.mark.asyncio
async def test_busy_bare_moa_returns_usage_without_queueing():
    runner, adapter = _make_runner(_session_entry())
    sk = _running(runner)
    reply = await runner._handle_message(MessageEvent(text="/moa", source=_make_source()))
    assert reply is not None and "/moa <prompt>" in reply
    assert runner._queue_depth(sk, adapter=adapter) == 0


@pytest.mark.asyncio
async def test_busy_moa_reports_missing_queue_storage():
    runner, adapter = _make_runner(_session_entry())
    sk = _running(runner)
    runner.adapters = {}
    reply = await runner._handle_message(MessageEvent(text="/moa review", source=_make_source()))
    assert reply is not None and "unavailable" in reply.lower()
    assert runner._queue_depth(sk, adapter=adapter) == 0


@pytest.mark.asyncio
async def test_busy_moa_adapter_bypass_does_not_interrupt_or_merge():
    import asyncio
    from tests.gateway.test_queue_consumption import _StubAdapter

    runner, _ = _make_runner(_session_entry())
    sk = _running(runner)
    adapter = _StubAdapter()
    adapter.gateway_runner = runner
    adapter._message_handler = runner._handle_message
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="ack"))
    adapter._active_sessions[sk] = asyncio.Event()
    runner.adapters = {Platform.TELEGRAM: adapter}
    for text in ("/moa first", "/moa second"):
        await adapter._handle_message_while_active(MessageEvent(text=text, source=_make_source()), sk)
    assert adapter.send.await_count == 2
    assert "MoA queued" in adapter.send.call_args.kwargs["content"]
    assert not adapter._active_sessions[sk].is_set()
    assert adapter.get_pending_message(sk).text == "/moa first"
    assert [event.text for event in runner._overflow_queue(sk)] == ["/moa second"]
    runner._running_agents[sk].interrupt.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("current_moa", [False, True])
async def test_moa_drain_defers_to_dispatch_and_preserves_fifo(current_moa):
    from tests.gateway.test_queue_consumption import _StubAdapter

    runner, _ = _make_runner(_session_entry())
    adapter = _StubAdapter()
    sk = build_session_key(_make_source())
    texts = ["ordinary next" if current_moa else "/moa first", "/moa second", "last"]
    if current_moa:
        runner._session_state(sk).conversation.model_override = {"provider": "moa"}
    for text in texts:
        runner._enqueue_fifo(sk, MessageEvent(text=text, source=_make_source()), adapter)
    for _ in range(2):
        pending = await runner._run_agent_drain_pending(
            {"final_response": "done"}, adapter, _make_source(), sk)
        assert pending == (None, None)
        assert adapter._pending_messages[sk].text == texts[0]
        assert [event.text for event in runner._overflow_queue(sk)] == texts[1:]


@pytest.mark.asyncio
@pytest.mark.parametrize("drain_initial", [False, True])
async def test_moa_real_dispatch_chain_restores_before_ordinary_followup(monkeypatch, drain_initial):
    from tests.gateway.test_queue_consumption import _StubAdapter

    runner, _ = _make_runner(_session_entry())
    adapter = _StubAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    sk = _running(runner)
    prior = {"provider": "openrouter", "model": "normal"}
    runner._session_state(sk).conversation.model_override = prior
    runner._evict_cached_agent = MagicMock()
    runner._persist_active_agents = MagicMock()
    runner._clear_durable_active_turn = AsyncMock()
    runner._run_post_turn_hooks = AsyncMock()
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    seen = []

    async def run_turn(event, source, key, generation):
        seen.append((event.text, runner._session_state(key).conversation.model_override["provider"]))
        pending = await runner._run_agent_drain_pending(
            {"final_response": "done"}, adapter, source, key)
        # MoA boundaries never recurse before the outer handler restores its model.
        assert pending == (None, None)
        return "done"

    runner._handle_message_with_agent = run_turn
    for text in ("/moa first", "/queue ordinary", "/moa second"):
        reply = await runner._handle_message(MessageEvent(text=text, source=_make_source()))
        assert reply is not None and "queued" in reply.lower()
    if drain_initial:
        assert await runner._run_agent_drain_pending(
            {"final_response": "initial done"}, adapter, _make_source(), sk) == (None, None)
    del runner._running_agents[sk]
    while sk in adapter._pending_messages:
        event = adapter.get_pending_message(sk)
        assert event is not None
        assert await runner._handle_message(event) == "done"
        assert runner._session_state(sk).conversation.model_override is prior
    assert seen == [("first", "moa"), ("ordinary", "openrouter"), ("second", "moa")]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
