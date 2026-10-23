"""Fresh-topic silence survives session admission and preparatory route probes."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.telegram_topic_sessions import TopicSpec, create_topic_session
from gateway.turn_context import TurnContext


@pytest.fixture
def opening(tmp_path, monkeypatch):
    cfg = {"model": {
        "default": "gpt-6.1-sol", "provider": "nous", "idle_revert_minutes": 15,
        "attended": {"model": "claude-opus-5-5", "provider": "custom:claude"},
    }}
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda *_: cfg)
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda _cfg=None: "gpt-6.1-sol")
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
        "provider": "nous", "api_key": "fixture", "api_mode": "chat_completions",
    })
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda provider, **_: {
        "provider": provider, "api_key": "fixture", "api_mode": "chat_completions",
    })
    monkeypatch.setattr("gateway.run._credential_pool_for_provider", lambda _: None)
    monkeypatch.setattr(runner, "_recover_telegram_topic_thread_id", lambda _: None)
    monkeypatch.setattr(runner, "_is_telegram_topic_lane", lambda _: True)
    monkeypatch.setattr(runner, "_record_telegram_topic_binding", lambda *_: None)
    monkeypatch.setattr(runner, "_load_reasoning_config", lambda *_args, **_kwargs: {})
    runner.hooks.emit = AsyncMock()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="owner",
                           chat_type="dm", thread_id="456")
    yield SimpleNamespace(runner=runner, source=source, cfg=cfg)
    if runner._executor:
        runner._executor.shutdown(wait=True)


def _turn(runner, source, key, cfg):
    turn = TurnRunner(runner, TurnContext(source=source, session_key=key, user_config=cfg))
    notices = []
    turn._emit_persistent_attended_notice = lambda message: notices.append(message)
    agent = SimpleNamespace(
        model="claude-opus-5-5", provider="custom:claude", api_mode="chat_completions",
        reasoning_config={}, _fallback_activated=False, _provider_fallback_active=False,
        _gateway_attended_decision=runner._session_state(key).conversation.last_attended_decision,
        context_compressor=None,
    )
    agent._gateway_attended_decision_key = agent._gateway_attended_decision.decision_key
    return turn, agent, notices


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "photo", "agent-created"])
async def test_first_route_is_quiet_but_real_later_switch_in_is_announced(opening, monkeypatch, kind):
    runner, source, cfg = opening.runner, opening.source, opening.cfg
    key = runner._session_key_for_source(source)
    if kind == "agent-created":
        adapter = SimpleNamespace(_create_dm_topic=AsyncMock(return_value="456"))
        monkeypatch.setattr(runner, "_delivery_adapter_for", lambda _: adapter)
        await create_topic_session(runner, source, TopicSpec(name="Room"))
    event = MessageEvent(
        text="hello", source=source, internal=kind == "agent-created",
        message_type=MessageType.PHOTO if kind == "photo" else MessageType.TEXT,
    )
    # Real admission tries before the entry exists, then retries after routing creates it.
    marked = await runner._mark_attended_user_activity(event, source, key)
    _, entry, key = await runner._hmwa_resolve_session(event, source)
    if not marked:
        await runner._mark_attended_user_activity(event, source, key)
    await runner._hmwa_open_session(entry, key, source)
    if kind == "photo":
        # Image-mode selection reads config, vision enrichment probes without a config,
        # and message assembly resolves with config again before the real agent starts.
        await asyncio.to_thread(runner._resolve_session_agent_runtime, session_key=key, user_config=cfg)
        await runner._resolve_session_agent_runtime_async(source=source, session_key=key)
    model, _ = await runner._resolve_session_agent_runtime_async(
        source=source, session_key=key, user_config=cfg,
    )
    state = runner._session_state(key).conversation
    assert model == "claude-opus-5-5"
    assert state.pending_attended_notice is None
    turn, agent, notices = _turn(runner, source, key, cfg)
    await asyncio.to_thread(turn._apply_attended_before_api_call, agent, commit=False)
    await asyncio.to_thread(turn._apply_attended_before_api_call, agent)
    assert notices == []
    assert (runner.session_store.get_last_user_activity_at(key) is None) == event.internal

    # Consume the same first-turn lifetime as normal inbound finalization.
    state.attended_new_session = False
    expired = datetime.now(timezone.utc) - timedelta(hours=1)
    runner.session_store.mark_user_activity(key, expired)
    state.last_user_activity_at = expired.timestamp()
    state.last_api_call_at = expired.timestamp()

    def switch(target, model, provider, *_args):
        target.model, target.provider = model, provider

    monkeypatch.setattr("agent.agent_runtime_helpers.switch_model", switch)
    await asyncio.to_thread(turn._apply_attended_before_api_call, agent)
    assert agent.model == "gpt-6.1-sol"
    notices.clear()
    reply = MessageEvent(text="I'm back", source=source)
    await runner._mark_attended_user_activity(reply, source, key)
    await asyncio.to_thread(turn._apply_attended_before_api_call, agent)
    assert agent.model == "claude-opus-5-5"
    assert notices == [
        "⇄ Switched to Opus 5.5 while you're active. Back to GPT-6.1 Sol after 15m idle.",
    ]
