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
async def test_reset_clears_deadline_even_if_config_opt_in_was_removed(harness):
    cfg, clock, source, restart = harness
    cfg["model"]["pick_ttl_hours"] = 12
    runner = restart()
    key = runner._session_key_for_source(source)
    await runner._handle_model_command(MessageEvent(text="/model picked", source=source))
    cfg["model"].pop("pick_ttl_hours")
    await runner._handle_model_command(MessageEvent(text="/model reset", source=source))
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
