"""Attended holds outlive idle windows without overriding explicit route choices."""
import asyncio
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pytest_asyncio

from gateway import run as gateway_run
from gateway import run_session_readback
from gateway.attended_model import expiry_time, resolve_attended_model
from gateway.config import GatewayConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import AsyncSessionStore, Platform, SessionEntry, SessionSource, SessionStore
from hermes_cli.commands import resolve_command, should_bypass_active_session
from hermes_cli.commands_platforms import telegram_bot_commands

CONFIG = {"model": {
    "default": "default", "provider": "nous", "idle_revert_minutes": 30,
    "attended": {"model": "opus", "provider": "anthropic", "reasoning_effort": "high"},
}}


@pytest_asyncio.fixture
async def attended(tmp_path, monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = SessionStore(tmp_path / "sessions", runner.config)
    runner._async_session_store = AsyncSessionStore(runner.session_store)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="attend", user_id="owner")
    entry = await runner.async_session_store.get_or_create_session(source)
    runner._load_reasoning_config = lambda *_args, **_kwargs: {}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: CONFIG)
    monkeypatch.setattr(runner, "_load_session_agent_runtime", lambda **kw: (
        (kw["attended_route"] or kw["override"] or {}).get("model", "default"), {}, None, None,
    ))
    yield runner, source, entry
    await asyncio.to_thread(runner.session_store.close_all_db_handles)


@pytest.mark.asyncio
async def test_busy_attend_persists_unpins_and_restarts_idle_window(attended, monkeypatch, tmp_path):
    runner, source, entry = attended
    key = entry.session_key
    state = runner._session_state(key)
    await runner.async_session_store.mark_user_activity(key, when=datetime.fromtimestamp(1))
    state.conversation.last_user_activity_at = 1.0
    cmd = resolve_command("attend")
    assert cmd is not None and should_bypass_active_session("attend")
    assert "attend" in dict(telegram_bot_commands())

    async def dispatch(text):
        return await runner._dispatch_busy_slash_command(MessageEvent(text=text, source=source), cmd, key, source)

    assert "Pinned" in await dispatch("/attend")
    assert "pinned" in await dispatch("/attend status")
    assert entry.attended_pinned
    assert SessionEntry.from_dict(entry.to_dict()).attended_pinned
    reader = SessionStore(tmp_path / "sessions", runner.config)
    try:
        restored = await asyncio.to_thread(reader.lookup_by_session_key, key)
        assert restored.attended_pinned
        decision = resolve_attended_model({
            "attended_pinned": restored.attended_pinned, "last_user_activity_at": 1,
            "default_model": "default",
        }, time.time(), CONFIG)
        assert decision.reason == "attended" and not decision.switched_back
        assert expiry_time(1, 30, pinned=True) is None
        assert not decision.idle_model
        before = time.time()
        assert "timer starts now" in await dispatch("/attend off")
        after = time.time()
        assert not entry.attended_pinned
        stamp = entry.last_user_activity_at.timestamp()
        assert before <= stamp <= after
        assert state.conversation.last_user_activity_at == stamp
        assert "not pinned" in await dispatch("/attend status")
        decision_state = {"last_user_activity_at": stamp, "default_model": "default"}
        assert resolve_attended_model(decision_state, stamp + 1799, CONFIG).reason == "attended"
        assert resolve_attended_model(decision_state, stamp + 1800, CONFIG).reason == "default"
        await asyncio.to_thread(reader.get_last_user_activity_at, key, refresh=True)
        assert not await asyncio.to_thread(reader.get_attended_pinned, key)
    finally:
        await asyncio.to_thread(reader.close_all_db_handles)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["session", "temporary", "once"])
async def test_pin_reaches_api_boundary_preserves_explicit_precedence_and_idle_picks(attended, monkeypatch, scope):
    runner, source, entry = attended
    key = entry.session_key
    await runner._handle_attend_command(MessageEvent(text="/attend", source=source))
    await runner.async_session_store.mark_user_activity(key, when=datetime.fromtimestamp(1))
    state = runner._session_state(key)
    state.conversation.last_user_activity_at = 1.0
    previous = resolve_attended_model({"last_user_activity_at": 1, "default_model": "default"}, 2, CONFIG)
    state.conversation.last_attended_decision = previous
    state.conversation.last_api_call_at = 1
    agent = SimpleNamespace(model="opus", provider="anthropic", api_mode="anthropic_messages",
                            _gateway_attended_decision_key=previous.decision_key)
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = SimpleNamespace(session_key=key, source=source, user_config=CONFIG)
    notices = turn._emit_persistent_attended_notice = Mock()
    switch = Mock()
    monkeypatch.setattr("agent.agent_runtime_helpers.switch_model", switch)
    await asyncio.to_thread(turn._apply_attended_before_api_call, agent)
    assert agent._gateway_attended_decision.reason == "attended"
    assert not agent._gateway_attended_decision.switched_back
    switch.assert_not_called()
    notices.assert_not_called()
    state.conversation.model_override = {"model": "picked", "provider": "nous"}
    state.conversation.model_override_scope = scope
    state.conversation.reasoning_override = {"enabled": True, "effort": "low"}
    state.conversation.reasoning_override_scope = "temporary"
    await runner.async_session_store.set_model_override(key, state.conversation.model_override, scope=scope)
    await asyncio.to_thread(runner._expire_attended_temporary_picks, key, CONFIG)
    await runner._expire_attended_temporary_picks_async(key, CONFIG)
    assert state.conversation.model_override["model"] == "picked"
    assert state.conversation.reasoning_override["effort"] == "low"
    decision = await asyncio.to_thread(runner._attended_resolution_for_session,
                                     source=source, session_key=key, user_config=CONFIG,
                                     override=state.conversation.model_override, allow_switch_back=True)
    assert (decision.model, decision.reason) == ("picked", "explicit")
    assert not decision.switched_back
    model, _, _ = await run_session_readback.read_session_settings(
        runner, source=source, session_key=key, user_config=CONFIG,
    )
    assert model == "picked"


@pytest.mark.asyncio
async def test_pin_rejects_disabled_config_and_bad_arguments_without_mutation(attended, monkeypatch):
    runner, source, entry = attended
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"model": {"default": "default"}})
    assert "not configured" in await runner._handle_attend_command(MessageEvent(text="/attend", source=source))
    assert not entry.attended_pinned
    assert "Usage:" in await runner._handle_attend_command(MessageEvent(text="/attend nope", source=source))
    assert "not pinned" in await runner._handle_attend_command(MessageEvent(text="/attend status", source=source))
    assert not entry.attended_pinned


@pytest.mark.asyncio
async def test_failed_pin_save_leaves_session_unchanged_and_reset_unpins(attended, monkeypatch):
    runner, source, entry = attended
    with monkeypatch.context() as patcher:
        patcher.setattr(runner.session_store, "_save_entry", Mock(side_effect=OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            await runner._handle_attend_command(MessageEvent(text="/attend", source=source))
    assert not entry.attended_pinned
    await runner._handle_attend_command(MessageEvent(text="/attend", source=source))
    reset = await asyncio.to_thread(runner.session_store.reset_session, entry.session_key)
    assert not reset.attended_pinned


def test_old_session_payload_defaults_unpinned_and_disabled_config_is_inert():
    entry = SessionEntry(session_id="legacy", session_key="legacy",
                         created_at=datetime.fromtimestamp(1), updated_at=datetime.fromtimestamp(1))
    assert not SessionEntry.from_dict(entry.to_dict()).attended_pinned
    decision = resolve_attended_model({"attended_pinned": True, "default_model": "default"}, 9999, {})
    assert decision.reason == "default"
