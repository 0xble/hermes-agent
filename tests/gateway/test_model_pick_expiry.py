"""Opt-in wall-clock model picks retain the legacy policy when unconfigured."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore


@pytest.fixture
def harness(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    cfg = {"model": {"default": "default", "provider": "openai"}}
    clock = SimpleNamespace(now=100_000.0)
    monkeypatch.setattr("time.time", lambda: clock.now)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda **_kw: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config: config["model"]["default"])
    runtime = {"provider": "openai", "api_key": "test-only", "base_url": "https://example.test/v1"}
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: dict(runtime))
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs_for_provider", lambda *_a, **_kw: dict(runtime))
    monkeypatch.setattr("hermes_cli.model_switch.resolve_display_context_length_async", AsyncMock(return_value=None))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="ttl-test", user_id="owner", chat_type="dm")

    def restart():
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        runner.session_store = SessionStore(sessions_dir=tmp_path / "sessions", config=runner.config)
        runner.session_store.get_or_create_session(source)
        runner._session_db = None
        runner._agent_cache_lock = None
        runner._evict_cached_agent = lambda _key: None
        runner._switch_cached_agent_model = lambda *_a: None
        runner._record_switch_metrics = lambda *_a: None
        runner._model_selection_warning = AsyncMock(return_value=None)
        result = SimpleNamespace(new_model="picked", target_provider="openai", api_key="test-only",
                                 base_url=runtime["base_url"], api_mode="chat_completions", provider_label="OpenAI",
                                 request_overrides={}, runtime_capabilities={}, model_info=None, warning_message=None)
        runner._perform_model_switch = AsyncMock(return_value=(result, None))
        return runner

    return cfg, clock, source, restart


@pytest.mark.asyncio
@pytest.mark.parametrize("attended", [False, True])
@pytest.mark.parametrize("ttl", [None, 0, -1, "bad", True, float("inf"), 12, 0.5])
@pytest.mark.parametrize("flag", ["", " --session", " --once", " --global"])
async def test_pick_policy_is_opt_in_and_explicit_flags_are_unchanged(harness, attended, ttl, flag, monkeypatch):
    cfg, clock, source, restart = harness
    if attended:
        cfg["model"]["attended"] = {"model": "attended"}
    if ttl is not None:
        cfg["model"]["pick_ttl_hours"] = ttl
    runner = restart()
    runner._channel_override_for = lambda _source: None
    monkeypatch.setattr("gateway.slash_commands_model._persist_model_switch_to_config", AsyncMock())
    reply = await runner._handle_model_command(MessageEvent(text="/model picked" + flag, source=source))
    key = runner._session_key_for_source(source)
    override = runner._session_model_override(key)
    enabled = not isinstance(ttl, bool) and isinstance(ttl, (int, float)) and 0 < ttl < float("inf")
    timed = enabled and not flag
    if flag == " --global":
        assert override is None
    else:
        assert (override or {}).get("expires_at") == (clock.now + ttl * 3600 if timed else None)
        assert runner._session_model_override_scope(key) == (
            "once" if flag == " --once" else "temporary" if not flag and (attended or timed) else "session")
    assert ("Expires" in reply) is timed
    if timed:
        assert f"{ttl:g}h" in reply
    if flag == " --once":
        assert runner.session_store.get_model_override(key) is None


@pytest.mark.parametrize("attended", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("ttl", [None, 12])
@pytest.mark.parametrize("picked_at", [
    100_000.0,
    # 12 elapsed hours cross the Los Angeles fall-back to 07:00 PST, not 08:00.
    datetime(2026, 10, 31, 20, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp(),
])
def test_pick_deadline_survives_restart_and_returns_to_normal_route(harness, attended, asynchronous, ttl, picked_at):
    cfg, clock, source, restart = harness
    clock.now = picked_at
    if attended:
        cfg["model"]["attended"] = {"model": "attended", "provider": "openai"}
    if ttl:
        cfg["model"]["pick_ttl_hours"] = ttl
    runner = restart()
    key = runner._session_key_for_source(source)
    asyncio.run(runner._handle_model_command(MessageEvent(text="/model picked", source=source)))
    deadline = clock.now + 12 * 3600
    persisted = runner.session_store.get_model_override(key)
    assert persisted.get("expires_at") == (str(deadline) if ttl else None)
    assert "api_key" not in persisted

    # Fresh process state and store exercise the durable hydration path, not just memory.
    runner = restart()
    runner.session_store.mark_user_activity(key, datetime.fromtimestamp(clock.now, timezone.utc))
    clock.now += 1800

    def resolve():
        if asynchronous:
            return asyncio.run(runner._resolve_session_agent_runtime_async(source=source, user_config=cfg))[0]
        return runner._resolve_session_agent_runtime(source=source, user_config=cfg)[0]

    assert resolve() == ("default" if attended and not ttl else "picked")
    if not ttl:
        return
    clock.now = deadline - 1
    assert resolve() == "picked"
    # At the exact wall-clock deadline it returns to the route live now, not the old default.
    cfg["model"]["default"] = "new-default"
    if attended:
        runner.session_store.mark_user_activity(key, datetime.fromtimestamp(clock.now, timezone.utc))
        runner._session_state(key).conversation.last_user_activity_at = clock.now
    clock.now = deadline
    assert resolve() == ("attended" if attended else "new-default")
    assert runner.session_store.get_model_override(key) is None
    notice = runner._session_state(key).conversation.pending_attended_notice
    assert "pick expired" in notice
    assert ("attended" if attended else "new-default") in notice
    assert runner._session_model_override(key) is None
    # The turn consumer clears this notice after emission. Later resolution must not re-arm it.
    runner._session_state(key).conversation.pending_attended_notice = None
    resolve()
    assert runner._session_state(key).conversation.pending_attended_notice is None
    # A second restart cannot resurrect the expired selection.
    runner = restart()
    assert runner.session_store.get_model_override(key) is None


@pytest.mark.asyncio
async def test_repick_restarts_deadline_and_stale_expiry_cannot_clear_it(harness):
    cfg, clock, source, restart = harness
    cfg["model"]["pick_ttl_hours"] = 12
    runner = restart()
    key = runner._session_key_for_source(source)
    event = MessageEvent(text="/model picked", source=source)
    await runner._handle_model_command(event)
    old = runner._session_model_override(key)
    clock.now += 3600
    await runner._handle_model_command(event)
    current = runner._session_model_override(key)
    assert current["expires_at"] == clock.now + 12 * 3600
    await runner._expire_session_model_pick(key, old)
    assert runner._session_model_override(key) is current
    assert runner.session_store.get_model_override(key)["expires_at"] == str(current["expires_at"])


@pytest.mark.asyncio
@pytest.mark.parametrize("after_restart", [False, True])
@pytest.mark.parametrize("past_deadline", [0, 1])
@pytest.mark.parametrize("role_hold", [False, True])
async def test_short_attended_turn_expires_before_assignment_and_emits_notice_now(
    harness, monkeypatch, after_restart, past_deadline, role_hold,
):
    from gateway.turn_context import TurnContext

    cfg, clock, source, restart = harness
    cfg["model"].update(pick_ttl_hours=12, attended={"model": "attended", "provider": "openai"})
    runner = restart()
    key = runner._session_key_for_source(source)
    expected_model = "attended"
    if role_hold:
        cfg["model"].pop("attended")
        cfg["model_presets"] = {"fast": {"model": "fast-model", "provider": "openai"}}
        expected_model = "fast-model"
    await runner._handle_model_command(MessageEvent(text="/model picked", source=source))
    deadline = runner._session_model_override(key)["expires_at"]
    if role_hold:
        runner.session_store.set_model_role(key, "fast")
        runner._session_state(key).conversation.model_role = "fast"
    if after_restart:
        runner = restart()
    runner._gateway_loop = asyncio.get_running_loop()
    clock.now = deadline + past_deadline
    runner.session_store.mark_user_activity(key, datetime.fromtimestamp(clock.now, timezone.utc))
    runner._session_state(key).conversation.last_user_activity_at = clock.now
    runner._get_proxy_url = lambda: None
    defaults = TurnContext()
    display = SimpleNamespace(**{name: getattr(defaults, name) for name in runner._DISPLAY_TO_TURN_CTX})
    display.user_config = cfg
    display.platform_key = "telegram"
    display.resolve_display_setting = lambda *_args: False
    runner._run_agent_display_settings = lambda _source: display
    runner._run_still_current_fn = lambda *_args: lambda: True
    captured = []

    class TurnAssembled(Exception):
        pass

    def capture_turn(ctx, turn, *_args):
        captured.append(turn)
        raise TurnAssembled

    runner._run_agent_bind_turn_wiring = capture_turn
    # No history, media or @refs: this path used to reach the worker before any lookup.
    with pytest.raises(TurnAssembled):
        await runner._run_agent_inner("hi", "", [], source, "sid", session_key=key)
    turn = captured[0]
    assert turn._model_assignment["override"] is None
    assert runner.session_store.get_model_override(key) is None
    notices, api_models = [], []
    monkeypatch.setattr(turn, "_emit_persistent_attended_notice", notices.append)
    monkeypatch.setattr(turn, "_combined_ephemeral_prompt", lambda: "")
    monkeypatch.setattr(turn, "_setup_stream_consumer", lambda _platform: (None, None, None, False))
    agent = SimpleNamespace(tools=[], context_compressor=None, session_prompt_tokens=0, session_completion_tokens=0)

    def resolve_agent(route, *_args):
        agent.model = route["model"]
        agent.provider = route["runtime"]["provider"]
        agent.api_mode = route["runtime"]["api_mode"]
        agent._gateway_attended_decision_key = runner._session_state(key).conversation.last_attended_decision.decision_key
        return agent, False

    def switch_model(agent, model, provider, _key, _url, api_mode, _capabilities):
        agent.model, agent.provider, agent.api_mode = model, provider, api_mode

    monkeypatch.setattr("agent.agent_runtime_helpers.switch_model", switch_model)
    monkeypatch.setattr(turn, "_resolve_turn_agent", resolve_agent)
    monkeypatch.setattr(turn, "_wire_turn_agent_callbacks", lambda *_args: None)
    monkeypatch.setattr(turn, "_load_turn_history", lambda *_args: ([], None, []))
    monkeypatch.setattr(turn, "_prepare_turn_message", lambda *_args: ("hi", 0.0))

    def conversation(*_args):
        turn._apply_attended_before_api_call(agent)
        api_models.append(agent.model)
        return {"final_response": "ok", "messages": [], "api_calls": 1}

    monkeypatch.setattr(turn, "_run_conversation_with_approval", conversation)
    monkeypatch.setattr(turn, "_finish_stream_consumer", lambda result, *_args: result)
    monkeypatch.setattr(turn, "_append_auto_media_tags", lambda response, *_args: response)
    monkeypatch.setattr(turn, "_sync_session_after_run", lambda *_args: (False, "sid", 0))
    runner._provider_routing = {}
    await asyncio.to_thread(turn.run_sync)
    assert api_models == [expected_model]
    assert notices == [f"⇄ Back to {expected_model}, model pick expired."]
    assert runner._session_state(key).conversation.pending_attended_notice is None


@pytest.mark.asyncio
@pytest.mark.parametrize("after_restart", [False, True])
async def test_reset_clears_deadline_even_if_config_opt_in_was_removed(harness, after_restart):
    cfg, clock, source, restart = harness
    cfg["model"]["pick_ttl_hours"] = 12
    runner = restart()
    key = runner._session_key_for_source(source)
    await runner._handle_model_command(MessageEvent(text="/model picked", source=source))
    cfg["model"].pop("pick_ttl_hours")
    if after_restart:
        runner = restart()
        assert runner._session_model_override(key) is None
    runner._perform_model_switch.reset_mock()
    await runner._handle_model_command(MessageEvent(text="/model reset", source=source))
    runner._perform_model_switch.assert_not_called()
    assert runner._session_model_override(key) is None
    assert runner.session_store.get_model_override(key) is None


@pytest.mark.asyncio
async def test_once_restores_original_deadline_and_scope_without_extending_them(harness):
    cfg, clock, source, restart = harness
    cfg["model"]["pick_ttl_hours"] = 12
    runner = restart()
    key = runner._session_key_for_source(source)
    await runner._handle_model_command(MessageEvent(text="/model picked", source=source))
    original = dict(runner._session_model_override(key))
    clock.now += 3600
    await runner._handle_model_command(MessageEvent(text="/model other --once", source=source))
    snapshot = runner._session_state(key).conversation.one_turn_restore
    runner._restore_session_model_override(key, snapshot)
    assert runner._session_model_override(key) == original
    assert runner._session_model_override_scope(key) == "temporary"
    clock.now = original["expires_at"]
    assert (await runner._resolve_session_agent_runtime_async(source=source, user_config=cfg))[0] == "default"
    assert key not in runner._pending_model_notes


@pytest.mark.asyncio
async def test_temporary_reasoning_keeps_idle_expiry_while_timed_model_survives(harness):
    cfg, clock, source, restart = harness
    cfg["model"].update(pick_ttl_hours=12, attended={"model": "attended"})
    runner = restart()
    key = runner._session_key_for_source(source)
    await runner._handle_model_command(MessageEvent(text="/model picked", source=source))
    runner._set_session_reasoning_override(key, {"enabled": True, "effort": "high"}, scope="temporary")
    runner.session_store.mark_user_activity(key, datetime.fromtimestamp(clock.now, timezone.utc))
    clock.now += 1800
    assert (await runner._resolve_session_agent_runtime_async(source=source, user_config=cfg))[0] == "picked"
    assert runner._session_reasoning_override(key) is None


@pytest.mark.parametrize("value", [True, -1, 0, "bad", float("nan"), float("inf")])
def test_invalid_persisted_deadlines_do_not_become_expiring_routes(value):
    from gateway.session import sanitize_model_override
    assert sanitize_model_override({"model": "picked", "expires_at": value, "api_key": "not-persisted"}) == {"model": "picked"}
