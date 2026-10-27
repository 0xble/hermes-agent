"""Fresh-topic silence survives session admission and preparatory route probes."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
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
@pytest.mark.parametrize("kind", ["text", "photo", "agent-created", "recovered-topic"])
@pytest.mark.parametrize("outcome", ["success", "exception", "interrupt"])
async def test_opening_turn_finalizes_before_later_switch_in(opening, monkeypatch, kind, outcome):
    runner, source, cfg = opening.runner, opening.source, opening.cfg
    key = runner._session_key_for_source(source)
    if kind == "agent-created":
        adapter = SimpleNamespace(_create_dm_topic=AsyncMock(return_value="456"))
        monkeypatch.setattr(runner, "_delivery_adapter_for", lambda _: adapter)
        await create_topic_session(runner, source, TopicSpec(name="Room"))
    if kind == "recovered-topic":
        # Internal lobby-shaped events bypass the lobby refusal, then recover a topic.
        source = replace(source, thread_id=None)
        monkeypatch.setattr(runner, "_recover_telegram_lobby_thread_id", lambda _: "456")
    quick_key = runner._session_key_for_source(source)
    event = MessageEvent(
        text="hello", source=source, internal=kind in {"agent-created", "recovered-topic"},
        message_type=MessageType.PHOTO if kind == "photo" else MessageType.TEXT,
    )
    monkeypatch.setattr(runner, "_is_user_authorized_for_source", lambda _: True)
    monkeypatch.setattr(runner, "_is_telegram_topic_root_lobby", lambda _: False)
    monkeypatch.setattr(runner, "_hm_try_passive_fyi_relay", AsyncMock(return_value=False))
    monkeypatch.setattr(runner, "_hmwa_run_session_hygiene", AsyncMock(return_value=[]))
    monkeypatch.setattr(runner, "_hmwa_first_contact_notes", AsyncMock())
    monkeypatch.setattr(runner, "_run_post_turn_hooks", AsyncMock())
    reached_request = asyncio.Event()
    continue_request = asyncio.Event()
    request = SimpleNamespace()

    async def prepare_message(**kwargs):
        # Admission, session recovery/creation and opening all run through the real handler.
        assert kwargs["session_key"] == key
        if kind == "photo":
            await asyncio.to_thread(runner._resolve_session_agent_runtime, session_key=key, user_config=cfg)
            await runner._resolve_session_agent_runtime_async(source=event.source, session_key=key)
        model, _ = await runner._resolve_session_agent_runtime_async(
            source=event.source, session_key=key, user_config=cfg,
        )
        state = runner._session_state(key).conversation
        assert model == "claude-opus-5-5"
        assert state.attended_new_session
        assert state.pending_attended_notice is None
        request.turn, request.agent, request.notices = _turn(runner, event.source, key, cfg)
        await asyncio.to_thread(request.turn._apply_attended_before_api_call, request.agent, commit=False)
        await asyncio.to_thread(request.turn._apply_attended_before_api_call, request.agent)
        assert request.notices == []
        assert (runner.session_store.get_last_user_activity_at(key) is None) == event.internal
        reached_request.set()
        if outcome == "exception":
            raise RuntimeError("opening failed")
        if outcome == "interrupt":
            await continue_request.wait()
        return None

    monkeypatch.setattr(runner, "_prepare_profile_scoped_inbound_message_text", prepare_message)
    prepare_turn = runner._hmwa_prepare_turn

    async def prepared_reply(*args):
        await prepare_turn(*args)
        return "opening reply", None

    monkeypatch.setattr(runner, "_hmwa_prepare_turn", prepared_reply)
    if outcome == "interrupt":
        task = asyncio.create_task(runner._handle_message(event))
        await asyncio.wait_for(reached_request.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "exception":
        with pytest.raises(RuntimeError, match="opening failed"):
            await runner._handle_message(event)
    else:
        assert await runner._handle_message(event) == "opening reply"
    assert reached_request.is_set()
    state = runner._session_state(key).conversation
    assert not state.attended_new_session
    assert not runner._session_state(quick_key).conversation.attended_new_session
    assert not runner._is_session_running(quick_key)

    # Seed elapsed idle time directly: a receipt cannot backdate durable activity.
    expired = datetime.now() - timedelta(hours=1)
    entry = runner.session_store.lookup_by_session_key(key)
    assert entry is not None
    entry.last_user_activity_at = expired
    runner.session_store.update_session(key, touch_activity=False)
    state.last_user_activity_at = expired.timestamp()
    state.last_api_call_at = expired.timestamp()

    def switch(target, model, provider, *_args):
        target.model, target.provider = model, provider

    monkeypatch.setattr("agent.agent_runtime_helpers.switch_model", switch)
    turn, agent, notices = request.turn, request.agent, request.notices
    await asyncio.to_thread(turn._apply_attended_before_api_call, agent)
    assert agent.model == "gpt-6.1-sol"
    notices.clear()
    reply = MessageEvent(text="I'm back", source=event.source)
    await runner._mark_attended_user_activity(reply, event.source, key)
    await asyncio.to_thread(turn._apply_attended_before_api_call, agent)
    assert agent.model == "claude-opus-5-5"
    assert notices == [
        "⇄ Switched to Opus 5.5 while you're active. Back to GPT-6.1 Sol after 15m idle.",
    ]
