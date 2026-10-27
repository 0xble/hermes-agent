"""Role routing at request, selection, persistence and conversation boundaries."""

import asyncio
import copy
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import hermes_yaml as yaml

from gateway.attended_model import resolve_attended_model
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.turn_context import TurnContext
from gateway.session import SessionSource
from gateway.telegram_topic_sessions import (
    TopicRequestError, TopicSpec, _apply_session_settings, _resolve_model,
)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    home.mkdir()
    cfg = {
        "model": {"default": "base", "provider": "baseline", "attended": {
            "model": "attended-model", "provider": "attended-provider", "idle_revert_minutes": 1,
        }},
        "model_presets": {
            "fast": {"model": "first", "provider": "provider-a", "reasoning_effort": "low"},
            "other": {"model": "second", "provider": "provider-b", "reasoning_effort": "high"},
        },
    }
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions", multiplex_profiles=True))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="owner", chat_type="dm")
    entry = runner.session_store.get_or_create_session(source)
    key = entry.session_key
    monkeypatch.setattr(runner, "_resolve_profile_home_for_source", lambda _: home)
    monkeypatch.setattr(runner, "_normalize_source_for_session_key", lambda source: source)
    monkeypatch.setattr(runner, "_evict_cached_agent", lambda _: None)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
        "provider": "baseline", "api_key": "fixture", "api_mode": "chat_completions",
    })
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda provider, **_: {
        "provider": provider, "api_key": "fixture", "api_mode": "chat_completions",
    })
    monkeypatch.setattr("gateway.run._credential_pool_for_provider", lambda _: None)
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda config=None: (config or cfg)["model"]["default"])
    yield SimpleNamespace(runner=runner, source=source, key=key, entry=entry, cfg=cfg, path=path, home=home)
    if runner._executor:
        runner._executor.shutdown(wait=True)


@pytest.fixture
def armed_loop_guard():
    import hermes_loop_guard
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    try:
        yield
    finally:
        hermes_loop_guard.set_loop_thread(None)


def _turn(lane):
    display = SimpleNamespace(**vars(TurnContext(
        user_config=copy.deepcopy(lane.cfg), resolve_display_setting=lambda *_: False,
    )), platform_key="telegram")
    _, turn, _ = lane.runner._run_agent_build_turn_context(
        display, None, message="hi", source=lane.source, session_key=lane.key, run_generation=None,
    )
    turn._emit_persistent_attended_notice = lambda _: None
    agent = SimpleNamespace(model="base", provider="baseline", api_mode="chat_completions",
                            _fallback_activated=False, _provider_fallback_active=False)
    return turn, agent


def _wire_role_provider(lane, monkeypatch, server):
    """Keep turn assembly, construction and request callbacks real, with loopback inference."""
    import run_agent
    import agent.model_metadata as metadata

    lane.cfg["model"].update(provider="custom", attended={})
    for preset in lane.cfg["model_presets"].values():
        preset["provider"] = "custom"
    lane.cfg["platform_toolsets"] = {"telegram": ["memory"]}
    lane.path.write_text(yaml.safe_dump(lane.cfg))
    runtime = {"provider": "custom", "api_key": "fake-loopback", "base_url": server.base_url,
               "api_mode": "chat_completions"}
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda *_a, **_k: lane.cfg)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: dict(runtime))
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda *_a, **_k: dict(runtime))
    monkeypatch.setattr(metadata, "get_model_context_length", lambda *_a, **_k: 128000)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-loopback")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    actual_agent = run_agent.AIAgent
    built = []

    class RouteAgent(actual_agent):
        def __init__(self, **kwargs):
            kwargs.update(skip_context_files=True, skip_memory=True, load_soul_identity=False)
            super().__init__(**kwargs)
            built.append((self, dict(kwargs)))

    monkeypatch.setattr(run_agent, "AIAgent", RouteAgent)
    return built


async def _run_role_message(lane):
    return await lane.runner._run_agent(
        message="CANARY role route", context_prompt="", history=[], source=lane.source,
        session_id=lane.entry.session_id, session_key=lane.key,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["role", "concrete"])
async def test_cold_selection_reaches_first_real_request(lane, monkeypatch, selection):
    from tests.fakes.fake_llm_provider import FakeLLMServer

    with FakeLLMServer(default_text="COLD-ROUTE") as server:
        built = _wire_role_provider(lane, monkeypatch, server)
        if selection == "role":
            lane.runner.session_store.set_model_role(lane.key, "fast")
        else:
            lane.runner.session_store.set_model_override(lane.key, {"model": "first", "provider": "custom"})
        try:
            result = await _run_role_message(lane)
            assert result["final_response"] == "COLD-ROUTE", result
            assert [request["model"] for request in server.main_requests()] == ["first"]
            assert built[0][1]["model"] == "first"
        finally:
            for agent, _ in built:
                agent.close()


@pytest.mark.asyncio
async def test_queued_role_edit_does_not_change_worker_startup(lane, monkeypatch):
    from tests.fakes.fake_llm_provider import FakeLLMServer

    with FakeLLMServer(default_text="QUEUED-ROUTE") as server:
        built = _wire_role_provider(lane, monkeypatch, server)
        assembled, release = asyncio.Event(), threading.Event()
        start = lane.runner._run_agent_start_turn_worker

        def delayed_start(ctx, work):
            def paused():
                assert release.wait(10), "worker release missing"
                return work()
            assembled.set()
            return start(ctx, paused)

        monkeypatch.setattr(lane.runner, "_run_agent_start_turn_worker", delayed_start)
        lane.runner._running_agents[lane.key] = SimpleNamespace()
        task = asyncio.create_task(_run_role_message(lane))
        try:
            await asyncio.wait_for(assembled.wait(), 5)
            status = await _apply_session_settings(lane.runner, lane.source, lane.entry, ("role", "fast"), None)
            assert status["model_status"] == "queued"
            assert status["model_effective_when"] == "next_turn"
            release.set()
            first = await task
            second = await _run_role_message(lane)
            assert first["final_response"] == second["final_response"] == "QUEUED-ROUTE"
            assert [kwargs["model"] for _, kwargs in built] == ["base", "first"]
            assert [request["model"] for request in server.main_requests()] == ["base", "first"]
        finally:
            release.set()
            if not task.done():
                await task
            for agent, _ in built:
                agent.close()


def _switch(agent, model, provider, *_args):
    agent.model, agent.provider = model, provider


async def _boundary(turn, agent):
    await asyncio.to_thread(turn._apply_attended_before_api_call, agent)
    return agent.model, agent.provider, agent.reasoning_config


@pytest.mark.asyncio
async def test_same_turn_reads_owning_profile_at_each_api_boundary(lane, monkeypatch):
    runner = lane.runner
    await runner._set_session_model_role(lane.key, "fast")
    turn, agent = _turn(lane)
    monkeypatch.setattr("agent.agent_runtime_helpers.switch_model", _switch)
    assert await _boundary(turn, agent) == ("first", "provider-a", {"enabled": True, "effort": "low"})
    lane.cfg["model_presets"]["fast"] = {
        "model": "edited", "provider": "provider-c", "reasoning_effort": "none",
    }
    lane.path.write_text(yaml.safe_dump(lane.cfg))
    assert await _boundary(turn, agent) == ("edited", "provider-c", {"enabled": False})
    assert turn._ctx.user_config["model_presets"]["fast"]["model"] == "first"


@pytest.mark.asyncio
async def test_same_turn_inherits_fresh_destination_reasoning(lane, monkeypatch):
    lane.cfg["model_presets"]["fast"].pop("reasoning_effort")
    lane.cfg["agent"] = {"reasoning_effort": "medium", "reasoning_overrides": {"first": "high"}}
    lane.path.write_text(yaml.safe_dump(lane.cfg))
    await lane.runner._set_session_model_role(lane.key, "fast")
    turn, agent = _turn(lane)
    monkeypatch.setattr("agent.agent_runtime_helpers.switch_model", _switch)
    assert await _boundary(turn, agent) == ("first", "provider-a", {"enabled": True, "effort": "high"})
    lane.cfg["agent"]["reasoning_overrides"]["first"] = "low"
    lane.path.write_text(yaml.safe_dump(lane.cfg))
    assert await _boundary(turn, agent) == ("first", "provider-a", {"enabled": True, "effort": "low"})


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["fast", "default", "attended", "deleted", "channel", "automatic"])
async def test_same_turn_destination_reasoning_precedence_and_disabled_values(lane, monkeypatch, route):
    lane.cfg["model_presets"]["fast"].pop("reasoning_effort")
    lane.cfg["agent"] = {"reasoning_effort": "medium", "reasoning_overrides": {}}
    if route in {"deleted", "channel"}:
        from gateway.config import ChannelOverride, PlatformConfig
        lane.runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(channel_overrides={
            "123": ChannelOverride(model="channel-model", provider="channel-provider"),
        })
    if route == "channel":
        lane.cfg["model"]["attended"].pop("model")
        lane.cfg["model"]["attended"].pop("provider")
    if route not in {"channel", "automatic"}:
        await lane.runner._set_session_model_role(lane.key, route)
    else:
        lane.runner._session_state(lane.key).conversation.last_user_activity_at = time.time()
    destination, provider = {
        "fast": ("first", "provider-a"), "default": ("base", "baseline"),
        "attended": ("attended-model", "attended-provider"),
        "deleted": ("channel-model", "channel-provider"),
        "channel": ("channel-model", "channel-provider"),
        "automatic": ("attended-model", "attended-provider"),
    }[route]
    lane.path.write_text(yaml.safe_dump(lane.cfg))
    turn, agent = _turn(lane)
    monkeypatch.setattr("agent.agent_runtime_helpers.switch_model", _switch)
    for raw, expected in [
        ("high", {"enabled": True, "effort": "high"}),
        (False, {"enabled": False}), ("none", {"enabled": False}),
        ({"enabled": False}, {"enabled": False}),
        ({"enabled": True, "effort": "thinking"}, {"enabled": True, "effort": "thinking"}),
    ]:
        lane.cfg["agent"]["reasoning_overrides"][destination] = raw
        lane.path.write_text(yaml.safe_dump(lane.cfg))
        assert await _boundary(turn, agent) == (destination, provider, expected)
    lane.cfg["agent"]["reasoning_overrides"].clear()
    lane.path.write_text(yaml.safe_dump(lane.cfg))
    assert (await _boundary(turn, agent))[2] == {"enabled": True, "effort": "medium"}
    lane.cfg["agent"]["reasoning_effort"] = False
    lane.path.write_text(yaml.safe_dump(lane.cfg))
    assert (await _boundary(turn, agent))[2] == {"enabled": False}


@pytest.mark.asyncio
async def test_role_declared_and_session_effort_outrank_destination_inheritance(lane, monkeypatch):
    lane.cfg["agent"] = {"reasoning_effort": "medium", "reasoning_overrides": {"first": "high"}}
    lane.cfg["model_presets"]["fast"]["reasoning_effort"] = False
    lane.path.write_text(yaml.safe_dump(lane.cfg))
    await lane.runner._set_session_model_role(lane.key, "fast")
    turn, agent = _turn(lane)
    monkeypatch.setattr("agent.agent_runtime_helpers.switch_model", _switch)
    assert (await _boundary(turn, agent))[2] == {"enabled": False}
    lane.cfg["model_presets"]["fast"]["reasoning_effort"] = "low"
    lane.path.write_text(yaml.safe_dump(lane.cfg))
    assert (await _boundary(turn, agent))[2] == {"enabled": True, "effort": "low"}
    lane.runner._set_session_reasoning_override(lane.key, {"enabled": False})
    turn, agent = _turn(lane)
    assert (await _boundary(turn, agent))[2] == {"enabled": False}
    lane.cfg["model_presets"]["fast"]["reasoning_effort"] = "ultra"
    lane.path.write_text(yaml.safe_dump(lane.cfg))
    assert (await _boundary(turn, agent))[2] == {"enabled": False}


@pytest.mark.asyncio
async def test_temporary_concrete_pick_expires_back_to_named_hold(lane, monkeypatch):
    runner = lane.runner
    await runner._set_session_model_role(lane.key, "fast")
    ctx = SimpleNamespace(session_key=lane.key, persist_global=False, current_model="first",
                          restore_snapshot=None, model_override_scope="temporary", pick_ttl_hours=0.0)
    result = SimpleNamespace(new_model="temporary", target_provider="provider-b", api_key="fixture",
                             base_url="", api_mode="chat_completions", request_overrides={},
                             runtime_capabilities={}, provider_label="B")
    await runner._record_model_switch(result, ctx, source=lane.source, one_turn=False, picker=False)
    assert runner.session_store.get_model_role(lane.key) == "fast"
    runner._session_state(lane.key).conversation.last_user_activity_at = time.time() - 3600
    await runner._expire_attended_temporary_picks_async(lane.key, lane.cfg)
    model, runtime = await runner._resolve_session_agent_runtime_async(
        source=lane.source, session_key=lane.key, user_config=lane.cfg)
    assert (model, runtime["provider"]) == ("first", "provider-a")


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True], ids=["topic-edit", "bulk-conversion"])
async def test_busy_role_assignment_waits_for_next_turn_but_config_stays_live(lane, monkeypatch, bulk):
    runner = lane.runner
    if bulk:
        runner.session_store.set_model_override(lane.key, {"model": "picked", "provider": "provider-a"})
        runner._session_model_overrides[lane.key] = {"model": "picked", "provider": "provider-a", "api_key": "fixture"}
    else:
        await runner._set_session_model_role(lane.key, "fast")
    turn, agent = _turn(lane)
    monkeypatch.setattr("agent.agent_runtime_helpers.switch_model", _switch)
    initial = await _boundary(turn, agent)
    # Assemble another turn, then edit while it is busy but before its first API call.
    turn, agent = _turn(lane)
    runner._running_agents[lane.key] = agent
    status = {}
    if bulk:
        assert await runner._migrate_telegram_model_overrides_to_role("other") == ([lane.key], [])
    else:
        status = await _apply_session_settings(runner, lane.source, lane.entry, ("role", "other"), None)
    assert await _boundary(turn, agent) == initial
    if not bulk:
        assert status["model_status"] == "queued"
        assert status["model_effective_when"] == "next_turn"
    if not bulk:
        lane.cfg["model_presets"]["fast"]["model"] = "edited-first"
        lane.path.write_text(yaml.safe_dump(lane.cfg))
        assert (await _boundary(turn, agent))[:2] == ("edited-first", "provider-a")
    runner._running_agents.pop(lane.key)
    model, runtime = await runner._resolve_session_agent_runtime_async(
        source=lane.source, session_key=lane.key, user_config=lane.cfg)
    assert (model, runtime["provider"]) == ("second", "provider-b")


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_role_edit_fences_stale_hydration_and_retires_once_restore(lane, bulk):
    runner = lane.runner
    runner.session_store.set_model_override(lane.key, {"model": "old", "provider": "provider-a"})
    epoch = runner._session_model_override_epoch(lane.key)
    stale = runner._load_session_model_override(lane.key)
    runner._claim_one_turn_restore(lane.key, {"had_override": True, "override": {"model": "old"}})
    if bulk:
        assert await runner._migrate_telegram_model_overrides_to_role("fast") == ([lane.key], [])
    else:
        await runner._set_session_model_role(lane.key, "fast")
    runner._commit_session_model_override(lane.key, stale, epoch)
    assert runner._session_model_override(lane.key) is None
    assert runner._session_state(lane.key).conversation.one_turn_restore is None
    runner._restore_pending_one_turn_model_override(lane.key)
    assert runner._session_model_override(lane.key) is None
    assert runner._session_model_override_epoch(lane.key) > epoch


def test_failed_bulk_snapshot_does_not_publish_or_leak_on_later_save(lane, monkeypatch):
    store = lane.runner.session_store
    store.set_model_override(lane.key, {"model": "old", "provider": "provider-a"})
    before = copy.deepcopy(store._entries[lane.key].to_dict())
    durable_before = store._db.load_gateway_routing_entries(scope=store._routing_scope())
    with monkeypatch.context() as faults:
        faults.setattr(store._db, "replace_gateway_routing_entries", lambda *a, **k: (_ for _ in ()).throw(OSError("primary")))
        faults.setattr(store, "_save_sessions_json", lambda *a: (_ for _ in ()).throw(OSError("fallback")))
        with pytest.raises(OSError, match="fallback"):
            store.set_model_roles([lane.key], "fast", clear_overrides=True)
    assert store._entries[lane.key].to_dict() == before
    store._save_entries()
    assert store._db.load_gateway_routing_entries(scope=store._routing_scope()) == durable_before


@pytest.mark.parametrize("role", ["attended", "deleted"])
def test_invalid_hold_uses_channel_default_even_with_recent_activity(role):
    cfg = {"model": {"attended": {"provider": "attended-provider"}}}
    decision = resolve_attended_model({"model_role": role, "last_user_activity_at": time.time(),
                                      "default_model": "base", "default_provider": "baseline",
                                      "channel_model": "channel", "channel_provider": "channel-provider"},
                                     time.time(), cfg)
    assert (decision.model, decision.provider, decision.reason) == ("channel", "channel-provider", "default")


def _guard_selection(lane, monkeypatch, guard):
    runner = lane.runner
    model = "llama-contributor" if guard == "data_policy" else "first"
    lane.cfg["model_presets"]["fast"].update(model=model, provider="openrouter")
    lane.path.write_text(yaml.safe_dump(lane.cfg))
    info = SimpleNamespace(provider_id="openrouter", has_cost_data=lambda: True,
                           cost_input=50 if guard == "cost" else 1, cost_output=1)
    result = SimpleNamespace(new_model=model, target_provider="openrouter", model_info=info,
                             api_key="fixture", base_url="", api_mode="chat_completions")
    monkeypatch.setattr(runner, "_perform_model_switch", AsyncMock(return_value=(result, None)))
    # Run the real registry and all 3 guards, with local metadata and measured context.
    monkeypatch.setattr(runner, "_cached_agent_for", lambda _: SimpleNamespace(
        model="old", context_compressor=SimpleNamespace(last_prompt_tokens=200_000 if guard == "context_cache" else 1)))
    monkeypatch.setattr("hermes_cli.model_selection_guards._context_cache_threshold", lambda: 100_000)
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["cost", "data_policy", "context_cache"])
async def test_slash_role_uses_shared_guard_and_confirmation_keeps_name(lane, monkeypatch, guard):
    runner = _guard_selection(lane, monkeypatch, guard)
    callbacks = []
    async def confirm(**kwargs):
        callbacks.append(kwargs["handler"])
        return "confirm required"
    monkeypatch.setattr(runner, "_request_slash_confirm", confirm)
    reply = await runner._handle_model_command_locked(MessageEvent(text="/model @fast", source=lane.source))
    assert reply == "confirm required"
    assert runner.session_store.get_model_role(lane.key) is None
    assert "@fast" in await callbacks[0]("always")
    assert runner.session_store.get_model_role(lane.key) == "fast"
    assert runner.session_store.get_model_override(lane.key) is None
    runner._perform_model_switch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["cost", "data_policy", "context_cache"])
async def test_topic_role_uses_shared_selection_guard(lane, monkeypatch, guard):
    runner = _guard_selection(lane, monkeypatch, guard)
    with pytest.raises(TopicRequestError, match="confirmation"):
        await _resolve_model(runner, lane.source, TopicSpec(model="@fast"), lane.key)
    assert runner.session_store.get_model_role(lane.key) is None
    runner._perform_model_switch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["new", "resume"])
@pytest.mark.parametrize("held", [False, True])
async def test_real_session_boundary_role_io_is_off_loop_and_unchanged_is_not_written(
    lane, monkeypatch, command, held,
):
    import hermes_loop_guard
    runner = lane.runner
    if held:
        await runner._set_session_model_role(lane.key, "fast")
    runner._session_db = SimpleNamespace(get_session_title=AsyncMock(return_value="target"))
    monkeypatch.setattr(runner, "_resolve_resume_target", AsyncMock(return_value=("target", "target")))
    monkeypatch.setattr(runner, "_resume_access_denied_reply", AsyncMock(return_value=None))
    monkeypatch.setattr(runner, "_cleanup_old_agent_for_reset", AsyncMock())
    monkeypatch.setattr(runner, "_fire_session_reset_hooks", AsyncMock())
    monkeypatch.setattr(runner, "_reset_notice_session_info", lambda _: "")
    monkeypatch.setattr(runner.session_store, "load_transcript", lambda _: [])
    # The older yolo cleanup is independently owned, and is not the role regression.
    monkeypatch.setattr(runner.session_store, "set_session_yolo", lambda *a: None)
    threads = []
    original = runner.session_store.set_model_role
    def record(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(runner.session_store, "set_model_role", record)
    owner = threading.get_ident()
    hermes_loop_guard.set_loop_thread(owner)
    try:
        event = MessageEvent(text="/new" if command == "new" else "/resume target", source=lane.source)
        if command == "new":
            await runner._handle_reset_command(event)
        else:
            await runner._handle_resume_command(event)
    finally:
        hermes_loop_guard.set_loop_thread(None)
    assert all(thread != owner for thread in threads)
    assert len(threads) == (1 if held and command == "new" else 0)
    # A role belongs to the routing lane across resume, but a fresh conversation drops it.
    assert runner.session_store.get_model_role(lane.key) == ("fast" if held and command == "resume" else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["attended", "deleted"])
async def test_invalid_hold_does_not_keep_previous_automatic_attended_route(lane, role):
    runner = lane.runner
    cfg = copy.deepcopy(lane.cfg)
    state = runner._session_state(lane.key).conversation
    state.last_user_activity_at = time.time()
    state.last_attended_decision = resolve_attended_model(
        {"last_user_activity_at": state.last_user_activity_at, "default_model": "base"}, time.time(), cfg)
    assert state.last_attended_decision.reason == "attended"
    await runner._set_session_model_role(lane.key, role)
    cfg["model"]["attended"]["model"] = ""
    decision = runner._attended_resolution_for_session(
        session_key=lane.key, source=lane.source, user_config=cfg, allow_switch_back=False)
    assert (decision.model, decision.provider, decision.reason) == ("base", "baseline", "default")


@pytest.mark.asyncio
async def test_compression_exhaustion_reset_does_not_reacquire_its_route_lock(lane, monkeypatch, armed_loop_guard):
    runner = lane.runner
    await runner._set_session_model_role(lane.key, "fast")
    monkeypatch.setattr(runner, "_sync_telegram_topic_binding", lambda *a, **k: None)
    monkeypatch.setattr(runner.session_store, "set_session_yolo", lambda *a: None)
    response, entry = await asyncio.wait_for(runner._hmwa_compression_exhaustion_reset(
        {"compression_exhausted": True}, "", lane.entry, lane.key, lane.source), 2)
    assert entry.session_id != lane.entry.session_id
    assert response
    assert await asyncio.to_thread(runner.session_store.get_model_role, lane.key) is None
    assert runner._session_state(lane.key).conversation.model_role is None


@pytest.mark.asyncio
async def test_auto_reset_clears_role_but_compression_repoint_keeps_it(lane, monkeypatch, armed_loop_guard):
    runner = lane.runner
    await runner._set_session_model_role(lane.key, "fast")
    repointed = await runner.async_session_store.switch_session(lane.key, "compressed-child")
    assert repointed.model_role == "fast"
    assert runner._session_state(lane.key).conversation.model_role == "fast"
    repointed.was_auto_reset = True
    monkeypatch.setattr(runner.session_store, "set_session_yolo", lambda *a: None)
    monkeypatch.setattr(runner.hooks, "emit", AsyncMock())
    assert await runner._hmwa_open_session(repointed, lane.key, lane.source) == (True, True)
    assert await asyncio.to_thread(runner.session_store.get_model_role, lane.key) is None
    assert runner._session_state(lane.key).conversation.model_role is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_role_write_uses_the_existing_override_lock(lane, monkeypatch, bulk):
    runner = lane.runner
    runner.session_store.set_model_override(lane.key, {"model": "old"})
    entered = asyncio.Event()
    lock = runner._session_model_override_lock(lane.key)
    def route_lock(key):
        assert key == lane.key
        entered.set()
        return lock
    monkeypatch.setattr(runner, "_session_model_override_lock", route_lock)
    async def edit():
        if bulk:
            return await runner._migrate_telegram_model_overrides_to_role("fast")
        await runner._set_session_model_role(lane.key, "fast")
    async with lock:
        task = asyncio.create_task(edit())
        try:
            await asyncio.wait_for(entered.wait(), 2)
        except TimeoutError:
            await task
            raise
        assert not task.done()
        assert runner.session_store.get_model_role(lane.key) is None
    result = await asyncio.wait_for(task, 2)
    if bulk:
        assert result == ([lane.key], [])
    assert runner.session_store.get_model_role(lane.key) == "fast"


@pytest.mark.asyncio
async def test_bulk_rechecks_role_eligibility_after_waiting_for_route_lock(lane, monkeypatch):
    runner = lane.runner
    runner.session_store.set_model_override(lane.key, {"model": "old"})
    lock = runner._session_model_override_lock(lane.key)
    waiting = asyncio.Event()
    def route_lock(key):
        assert key == lane.key
        waiting.set()
        return lock
    monkeypatch.setattr(runner, "_session_model_override_lock", route_lock)
    async with lock:
        task = asyncio.create_task(runner._migrate_telegram_model_overrides_to_role("other"))
        await asyncio.wait_for(waiting.wait(), 2)
        # The earlier serialized edit publishes while bulk is waiting on its stale listing.
        await asyncio.to_thread(runner.session_store.set_model_role, lane.key, "fast", clear_overrides=True)
        runner._session_state(lane.key).conversation.model_role = "fast"
    assert await asyncio.wait_for(task, 2) == ([], [])
    assert runner.session_store.get_model_role(lane.key) == "fast"
    assert runner._session_state(lane.key).conversation.model_role == "fast"


@pytest.mark.asyncio
@pytest.mark.parametrize("global_pick", [False, True], ids=["session", "global"])
async def test_concrete_role_clear_settles_before_newer_topic_role_edit(lane, monkeypatch, global_pick):
    from hermes_cli.model_switch import ModelSwitchResult

    runner = lane.runner
    await runner._set_session_model_role(lane.key, "fast")
    ctx = SimpleNamespace(session_key=lane.key, persist_global=global_pick, current_model="first",
                          config_path=lane.path, restore_snapshot=None, model_override_scope="session",
                          pick_ttl_hours=0.0)
    result = ModelSwitchResult(success=True, new_model="concrete", target_provider="provider-c",
                               api_key="fixture", base_url="", api_mode="chat_completions")
    clearing, waiting = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    writes = []
    original = runner.session_store.set_model_role

    def blocked_clear(key, role, **kwargs):
        writes.append(role)
        if role is None:
            loop.call_soon_threadsafe(clearing.set)
            assert release.wait(10)
        return original(key, role, **kwargs)

    monkeypatch.setattr(runner.session_store, "set_model_role", blocked_clear)
    lock = runner._session_model_override_lock(lane.key)

    def route_lock(key):
        assert key == lane.key
        task = asyncio.current_task()
        if task is not None and task.get_name() == "newer-topic-role":
            waiting.set()
        return lock

    monkeypatch.setattr(runner, "_session_model_override_lock", route_lock)
    older = asyncio.create_task(runner._record_model_switch(
        result, ctx, source=lane.source, one_turn=False, picker=False))
    newer = None
    try:
        await asyncio.wait_for(clearing.wait(), 2)
        assert lock.locked()
        newer = asyncio.create_task(_apply_session_settings(
            runner, lane.source, lane.entry, ("role", "other"), None), name="newer-topic-role")
        await asyncio.wait_for(waiting.wait(), 2)
        assert not newer.done()
        assert writes == [None]
        assert runner.session_store.get_model_role(lane.key) == "fast"
        assert runner._session_state(lane.key).conversation.model_role == "fast"
    finally:
        release.set()
        await asyncio.wait_for(older, 2)
        if newer is not None:
            await asyncio.wait_for(newer, 2)
    assert writes == [None, "other"]
    assert not lock.locked()
    assert runner.session_store.get_model_role(lane.key) == "other"
    assert runner._session_state(lane.key).conversation.model_role == "other"
    assert runner.session_store.get_model_override(lane.key) is None
    assert runner._session_model_override(lane.key) is None
    durable = runner.session_store._db.load_gateway_routing_entries(scope=runner.session_store._routing_scope())
    assert json.loads(durable[lane.key])["model_role"] == "other"
    mirror = json.loads((runner.session_store.sessions_dir / "sessions.json").read_text())
    assert mirror[lane.key]["model_role"] == "other"
    model, runtime = await runner._resolve_session_agent_runtime_async(
        source=lane.source, session_key=lane.key, user_config=lane.cfg)
    assert (model, runtime["provider"]) == ("second", "provider-b")
    if global_pick:
        assert yaml.safe_load(lane.path.read_text())["model"]["default"] == "concrete"


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_cancelled_role_write_keeps_lock_until_worker_and_memory_settle(lane, monkeypatch, bulk):
    runner = lane.runner
    runner.session_store.set_model_override(lane.key, {"model": "old"})
    entered, settled = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = runner.session_store.set_model_roles
    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(runner.session_store, "set_model_roles", blocked)
    edit = (runner._migrate_telegram_model_overrides_to_role("fast") if bulk
            else runner._set_session_model_role(lane.key, "fast"))
    task = asyncio.create_task(edit)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        loop.call_soon(settled.set)
        await settled.wait()
        assert not task.done()
        assert runner._session_model_override_lock(lane.key).locked()
        assert runner.session_store.get_model_role(lane.key) is None
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runner._session_model_override_lock(lane.key).locked()
    assert runner.session_store.get_model_role(lane.key) == "fast"
    assert runner._session_state(lane.key).conversation.model_role == "fast"
