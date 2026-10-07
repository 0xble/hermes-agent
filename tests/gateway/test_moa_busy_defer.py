"""Gateway /moa while a turn is running: defer, replay through idle dispatch, restore."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli.commands import resolve_command

STANDING = {"provider": "openrouter", "model": "standing-model"}


class _Adapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False): pass
    async def disconnect(self): pass
    async def send(self, *args, **kwargs): pass
    async def get_chat_info(self, *args, **kwargs): return {}


def _event(text: str) -> MessageEvent:
    source = SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1",
                           user_name="tester", chat_type="dm")
    return MessageEvent(text=text, source=source)


def _adapter():
    adapter = _Adapter(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)
    adapter._send_with_retry = AsyncMock()
    return adapter


def _runner(adapter):
    runner = object.__new__(GatewayRunner)
    runner.session_store = None
    runner.config = None
    runner._busy_input_mode = "queue"
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda _source: adapter
    runner._evict_cached_agent = lambda _key: None
    return runner


@pytest.mark.asyncio
async def test_deferred_moa_replays_through_idle_handler_and_restores_standing_override(monkeypatch):
    adapter = _adapter()
    runner = _runner(adapter)
    event = _event("/moa compare these answers")
    key = build_session_key(event.source)
    runner._session_state(key).conversation.model_override = dict(STANDING)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"moa": {"default_preset": "default"}})

    reply = await runner._dispatch_busy_slash_command(event, resolve_command("moa"), key, event.source)
    assert "scheduled" in str(reply).lower() or reply is None
    # The running turn's model is untouched while the command waits.
    assert runner._session_state(key).conversation.model_override == STANDING
    replay = adapter._pop_deferred_command(key)
    assert replay is event

    # Replay goes through the idle canonical dispatcher, which rewrites the turn to the payload.
    handled, result = await runner._hm_dispatch_canonical_command(replay, replay.source, key, "moa")
    assert (handled, result) == (False, None)
    assert replay.text == "compare these answers"
    state = runner._session_state(key)
    assert state.conversation.model_override["provider"] == "moa"
    assert state.conversation.one_turn_restore == {"had_override": True, "override": STANDING}

    # The real turn finalizer restores the standing /model override.
    generation = runner._begin_session_run_generation(key)
    runner._restore_pending_one_turn_model_override(key, generation)
    assert state.conversation.model_override == STANDING
    assert state.conversation.one_turn_restore is None


@pytest.mark.asyncio
async def test_stop_or_new_invalidation_drops_deferred_moa():
    adapter = _adapter()
    runner = _runner(adapter)
    event = _event("/moa compare these answers")
    key = build_session_key(event.source)
    await runner._dispatch_busy_slash_command(event, resolve_command("moa"), key, event.source)
    # /stop, /new and /reset call this adapter hook (gateway/run_agent_cache.py).
    adapter._invalidate_deferred_commands(key)
    adapter.resume_deferred_commands(key)
    await asyncio.sleep(0)
    assert adapter._pop_deferred_command(key) is None
    assert runner._session_state(key).conversation.model_override is None


@pytest.mark.asyncio
async def test_queue_mode_in_turn_drain_takes_text_before_deferred_moa():
    """Documented ordering: queued plain text drains inside the running turn; deferred /moa
    waits for the turn release. Text sent after /moa therefore runs first, on the prior model."""
    adapter = _adapter()
    runner = _runner(adapter)
    runner._draining = False
    moa = _event("/moa compare these answers")
    key = build_session_key(moa.source)
    await runner._dispatch_busy_slash_command(moa, resolve_command("moa"), key, moa.source)
    adapter._pending_messages[key] = _event("plain follow-up")
    runner._promote_queued_event = lambda _key, _adapter, pending: pending
    runner._pending_event_audio_paths = lambda _event: []

    pending_event, pending = await runner._run_agent_drain_pending(
        {"final_response": "done"}, adapter, moa.source, key)
    assert pending == "plain follow-up"
    assert adapter._pop_deferred_command(key) is moa
    # The deferred /moa has not touched the model the drained text runs on.
    assert runner._session_state(key).conversation.model_override is None
