"""Attended holds outlive idle windows without overriding explicit route choices."""
import asyncio
import time
import threading
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


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["repin", "handoff", "resume", "branch", "reset"])
@pytest.mark.parametrize("duration", ["", " 2h"])
async def test_attended_pin_follows_repins_but_not_conversation_boundaries(attended, tmp_path, transition, duration):
    runner, source, entry = attended
    key = entry.session_key
    await runner._handle_attend_command(MessageEvent(text="/attend" + duration, source=source))
    deadline = entry.attended_pin_until
    if transition == "reset":
        switched = await asyncio.to_thread(runner.session_store.reset_session, key)
    else:
        kwargs = {"expected_session_id": entry.session_id}
        if transition == "handoff":
            kwargs["preserve_prompt_pin"] = False
        if transition in {"resume", "branch"}:
            kwargs["preserve_attended_pin"] = False
        switched = await runner.async_session_store.switch_session(key, "retargeted", **kwargs)
    expected = transition in {"repin", "handoff"}
    assert switched is not None and switched.attended_pinned is expected
    assert switched.attended_pin_until == (deadline if expected else None)
    assert await asyncio.to_thread(runner.session_store.get_attended_pinned, key) is expected
    reader = SessionStore(tmp_path / "sessions", runner.config)
    try:
        restored = await asyncio.to_thread(reader.lookup_by_session_key, key)
        assert restored.attended_pinned is expected
        assert restored.attended_pin_until == (deadline if expected else None)
    finally:
        await asyncio.to_thread(reader.close_all_db_handles)


@pytest.mark.asyncio
async def test_attend_off_without_pin_does_not_restart_idle_window(attended):
    runner, source, entry = attended
    key = entry.session_key
    await runner.async_session_store.mark_user_activity(key, when=datetime.fromtimestamp(1))
    state = runner._session_state(key)
    state.conversation.last_user_activity_at = 1.0
    stamp = entry.last_user_activity_at
    reply = await runner._handle_attend_command(MessageEvent(text="/attend off", source=source))
    assert "not pinned" in reply
    assert not entry.attended_pinned
    assert entry.last_user_activity_at == stamp
    assert state.conversation.last_user_activity_at == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize("pin_during_resolution", [False, True])
async def test_async_route_resolution_reads_attended_pin_off_loop(attended, monkeypatch, pin_during_resolution):
    runner, source, entry = attended
    event = MessageEvent(text="/attend", source=source)
    if not pin_during_resolution:
        await runner._handle_attend_command(event)
    await runner.async_session_store.mark_user_activity(entry.session_key, when=datetime.fromtimestamp(1))
    loop_thread = threading.get_ident()
    getter = runner.session_store.get_attended_pinned
    reads = []

    def checked_getter(key):
        assert threading.get_ident() != loop_thread
        reads.append(key)
        return getter(key)

    monkeypatch.setattr(runner.session_store, "get_attended_pinned", checked_getter)
    loop = asyncio.get_running_loop()
    loader = runner._load_session_agent_runtime
    changed = False

    def racing_loader(**kwargs):
        nonlocal changed
        if pin_during_resolution and not changed:
            changed = True
            asyncio.run_coroutine_threadsafe(runner._handle_attend_command(event), loop).result(timeout=10)
        return loader(**kwargs)

    monkeypatch.setattr(runner, "_load_session_agent_runtime", racing_loader)
    model, _runtime = await runner._resolve_session_agent_runtime_async(
        source=source, session_key=entry.session_key, user_config=CONFIG,
    )
    assert reads and model == "opus"


def test_old_session_payload_defaults_unpinned_and_disabled_config_is_inert():
    entry = SessionEntry(session_id="legacy", session_key="legacy",
                         created_at=datetime.fromtimestamp(1), updated_at=datetime.fromtimestamp(1))
    assert not SessionEntry.from_dict(entry.to_dict()).attended_pinned
    decision = resolve_attended_model({"attended_pinned": True, "default_model": "default"}, 9999, {})
    assert decision.reason == "default"


@pytest.mark.parametrize("stamp", [None, 1, 2400])
def test_timed_pin_resolution_uses_deadline_or_newer_user_activity(stamp):
    from gateway.attended_model import expiry_time

    state = {"attended_pinned": True, "attended_pin_until": 1800,
             "last_user_activity_at": stamp, "default_model": "default"}
    deadline = max(stamp or 0, 1800) + 1800
    assert expiry_time(stamp, 30, pinned=True, pin_until=1800) == deadline
    for clock in (1799, max(1800, stamp or 0), deadline - 1):
        decision = resolve_attended_model(state, clock, CONFIG)
        assert decision.reason == "attended"
        assert bool(decision.idle_model) is (clock >= 1800)
    assert resolve_attended_model(state, deadline, CONFIG).reason == "default"
    state["model_override"] = "explicit"
    assert resolve_attended_model(state, deadline, CONFIG).model == "explicit"


@pytest.mark.asyncio
@pytest.mark.parametrize("duration,seconds", [("10s", 10), ("30m", 1800), ("2h", 7200), ("1h30m", 5400), ("1d", 86400)])
async def test_timed_busy_pin_replaces_deadline_and_round_trips(attended, monkeypatch, tmp_path, duration, seconds):
    from zoneinfo import ZoneInfo

    runner, source, entry = attended
    clock = 1_800_000_000.0
    monkeypatch.setattr("gateway.slash_commands_attend.time.time", lambda: clock)
    monkeypatch.setattr("gateway.slash_commands_attend.get_timezone", lambda: ZoneInfo("America/Los_Angeles"))
    cmd = resolve_command("attend")

    async def dispatch(args):
        return await runner._dispatch_busy_slash_command(
            MessageEvent(text="/attend" + args, source=source), cmd, entry.session_key, source,
        )

    reply = await dispatch(" " + duration)
    deadline = clock + seconds
    assert "Pinned" in reply and duration in reply
    local_release = datetime.fromtimestamp(deadline, ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %I:%M %p %Z")
    assert local_release in reply
    assert entry.attended_pinned and entry.attended_pin_until == deadline
    assert SessionEntry.from_dict(entry.to_dict()).attended_pin_until == deadline
    reader = SessionStore(tmp_path / "sessions", runner.config)
    try:
        restored = await asyncio.to_thread(reader.lookup_by_session_key, entry.session_key)
        assert restored.attended_pin_until == deadline
        route = resolve_attended_model({**restored.to_dict(), "default_model": "default"}, deadline - 1, CONFIG)
        assert route.reason == "attended" and not route.idle_model
    finally:
        await asyncio.to_thread(reader.close_all_db_handles)
    status = await dispatch(" status")
    assert "more" in status and local_release in status
    if seconds < 60:
        assert "<1m" in status
    clock += 10
    await dispatch(" 1h30m")
    assert entry.attended_pin_until == clock + 5400
    await dispatch("")
    assert entry.attended_pinned and entry.attended_pin_until is None
    assert "permanent" in await dispatch(" status")
    await dispatch(" 30m")
    await dispatch(" off")
    assert not entry.attended_pinned and entry.attended_pin_until is None


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", ["0m", "0h0m", "-2h", "2", "1h-30m", "1h30", "30m junk", "nan", "infh", "1e5h", "9" * 400 + "d"])
async def test_invalid_duration_does_not_change_existing_pin(attended, duration):
    runner, source, entry = attended
    await runner._handle_attend_command(MessageEvent(text="/attend 2h", source=source))
    before = entry.to_dict()
    reply = await runner._handle_attend_command(MessageEvent(text="/attend " + duration, source=source))
    assert "Usage:" in reply
    assert entry.to_dict() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("has_activity", [False, True])
async def test_timed_pin_expires_lazily_at_api_boundary_after_full_idle_window(attended, monkeypatch, has_activity):
    runner, source, entry = attended
    key = entry.session_key
    clock = 1_800_000_000.0
    monkeypatch.setattr("gateway.slash_commands_attend.time.time", lambda: clock)
    if has_activity:
        await runner.async_session_store.mark_user_activity(key, when=datetime.fromtimestamp(1))
    await runner._handle_attend_command(MessageEvent(text="/attend 30m", source=source))
    deadline = entry.attended_pin_until
    state = runner._session_state(key)
    state.conversation.last_user_activity_at = 1.0 if has_activity else None
    state.conversation.last_api_call_at = 1.0
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = SimpleNamespace(session_key=key, source=source, user_config=CONFIG)
    turn._emit_persistent_attended_notice = Mock()
    switch = Mock()
    monkeypatch.setattr("agent.agent_runtime_helpers.switch_model", switch)
    agent = SimpleNamespace(model="opus", provider="anthropic", api_mode="anthropic_messages")

    # Initialize the existing request-route key, which may apply attended reasoning.
    await asyncio.to_thread(turn._apply_attended_before_api_call, agent)
    assert agent._gateway_attended_decision.reason == "attended"
    switch.reset_mock()
    turn._emit_persistent_attended_notice.reset_mock()
    for clock in (deadline - 1, deadline, deadline + 1799):
        await asyncio.to_thread(turn._apply_attended_before_api_call, agent)
        assert agent._gateway_attended_decision.reason == "attended"
        model, _runtime = await runner._resolve_session_agent_runtime_async(source=source, session_key=key, user_config=CONFIG)
        assert model == "opus"
        status = await runner._handle_attend_command(MessageEvent(text="/attend status", source=source))
        if clock >= deadline:
            assert "not pinned" in status
    switch.assert_not_called()
    # Inspection must not restart the idle window or persist expiry at observation time.
    assert entry.attended_pin_until == deadline
    clock = deadline + 1800
    # A quiet request boundary remains required, independently of the timed pin.
    state.conversation.last_api_call_at = 1.0
    await asyncio.to_thread(turn._apply_attended_before_api_call, agent)
    assert agent._gateway_attended_decision.reason == "default"
    assert agent._gateway_attended_decision.switched_back
    assert switch.call_count == 1
    assert turn._emit_persistent_attended_notice.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("async_expiry", [False, True])
async def test_timed_pin_keeps_temporary_model_and_reasoning_until_deadline_plus_idle(attended, monkeypatch, async_expiry):
    runner, source, entry = attended
    key = entry.session_key
    clock = 1_800_000_000.0
    monkeypatch.setattr("gateway.slash_commands_attend.time.time", lambda: clock)
    await runner._handle_attend_command(MessageEvent(text="/attend 30m", source=source))
    deadline = entry.attended_pin_until
    state = runner._session_state(key)
    state.conversation.last_user_activity_at = 1.0
    state.conversation.model_override = {"model": "picked", "provider": "nous"}
    state.conversation.model_override_scope = "temporary"
    state.conversation.reasoning_override = {"enabled": True, "effort": "low"}
    state.conversation.reasoning_override_scope = "temporary"
    await runner.async_session_store.set_model_override(key, state.conversation.model_override, scope="temporary")

    async def expire():
        if async_expiry:
            await runner._expire_attended_temporary_picks_async(key, CONFIG)
        else:
            await asyncio.to_thread(runner._expire_attended_temporary_picks, key, CONFIG)

    clock = deadline + 1799
    await expire()
    assert state.conversation.model_override["model"] == "picked"
    assert state.conversation.reasoning_override["effort"] == "low"
    model, _, _ = await run_session_readback.read_session_settings(runner, source=source, session_key=key, user_config=CONFIG)
    assert model == "picked"
    clock = deadline + 1800
    await expire()
    assert state.conversation.model_override is None
    assert state.conversation.reasoning_override is None
