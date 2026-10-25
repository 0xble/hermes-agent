"""Topic results distinguish the requested pick from the destination's runtime route."""

import asyncio
import copy
import threading
import time
from datetime import datetime, timezone

import pytest

from gateway.config import ChannelOverride, Platform
from gateway.telegram_topic_sessions import TopicSpec, create_topic_session, edit_topic_session
from tests.gateway.test_telegram_topic_tool import FakeTelegram, _runner, _seed_caller, _source, home as home


def _runtime(monkeypatch, runner, config):
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda *_: config)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
        "provider": "default-provider", "api_key": "fixture",
    })
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda provider, **_: {
        "provider": provider, "api_key": "fixture",
    })
    monkeypatch.setattr("gateway.run._credential_pool_for_provider", lambda _: None)
    del runner._load_reasoning_config


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "name", "icon", "reasoning", "pick"])
@pytest.mark.parametrize("route", ["default", "implicit_effort", "channel", "session", "attended", "role", "profile"])
async def test_topic_readback_uses_runtime_precedence(home, monkeypatch, operation, route):
    runner = _runner(home, FakeTelegram())
    config = {
        "model": {"default": "default-model", "provider": "default-provider"},
        "agent": {"reasoning_effort": "low", "reasoning_overrides": {
            "channel-model": "high", "session-model": "xhigh", "attended-model": "high",
            "gpt-test": "medium",
        }},
    }
    _runtime(monkeypatch, runner, config)
    loaded_paths = []
    if route == "profile":
        profile_home = home.path / "profiles" / "work"
        profile_home.mkdir(parents=True)
        runner.config.multiplex_profiles = True
        runner._resolve_profile_home_for_source = lambda _: profile_home

        def routed_config(path=None):
            loaded_paths.append(path)
            return config if path == profile_home / "config.yaml" else {
                "model": {"default": "wrong-launch-model"},
            }

        monkeypatch.setattr("gateway.run._load_gateway_config", routed_config)
    source = _source(thread_id="4242")
    key = runner._session_key_for_source(source)
    entry = await _seed_caller(runner, home, thread_id="4242")
    expected_model, expected_provider, expected_effort = "default-model", "default-provider", "low"
    if route == "implicit_effort":
        config["agent"] = {}
        expected_effort = "medium"
    if route in {"channel", "session"}:
        runner.config.platforms[Platform.TELEGRAM].channel_overrides = {
            "4242": ChannelOverride(model="channel-model", provider="channel-provider"),
        }
        expected_model, expected_provider, expected_effort = "channel-model", "channel-provider", "high"
    if route == "session":
        runner.session_store.set_model_override(key, {"model": "session-model", "provider": "session-provider"})
        expected_model, expected_provider, expected_effort = "session-model", "session-provider", "xhigh"
    if route in {"attended", "role"}:
        config["model"]["attended"] = {"model": "attended-model", "provider": "attended-provider",
                                           "reasoning_effort": "high"}
        if route == "attended":
            config["model"]["idle_revert_minutes"] = 15
            runner.session_store.mark_user_activity(key, datetime.now(timezone.utc))
        else:
            runner.session_store.set_model_role(key, "attended")
        expected_model, expected_provider, expected_effort = "attended-model", "attended-provider", "high"
    if operation == "create":
        result = await create_topic_session(runner, _source(), TopicSpec(name="Room", icon="💡"))
    else:
        spec = {"name": TopicSpec(name="Renamed"), "icon": TopicSpec(icon="🧵"),
                "reasoning": TopicSpec(reasoning="none"), "pick": TopicSpec(model="gpt-test")}[operation]
        if operation == "reasoning":
            expected_effort = "none"
        if operation == "pick":
            expected_model, expected_provider, expected_effort = "gpt-test", "openai-codex", "medium"
        result = await edit_topic_session(runner, _source(), "4242", spec)
    assert result["session_id"] == entry.session_id
    assert (result["effective_model"], result["effective_provider"], result["effective_effort"]) == (
        expected_model, expected_provider, expected_effort,
    )
    assert (result["model"], result["provider"]) == (
        ("gpt-test", "openai-codex") if operation == "pick" else (None, None)
    )
    assert result["model_status"] == ("applied" if operation == "pick" else "unchanged")
    if route == "profile":
        assert loaded_paths and all(path == profile_home / "config.yaml" for path in loaded_paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_new_sessions", [True, False])
async def test_unseeded_create_readback_matches_attended_first_turn(home, monkeypatch, start_new_sessions):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    runner = _runner(home, FakeTelegram(thread_id=6001))
    config = {
        "model": {"default": "default-model", "provider": "default-provider", "attended": {
            "model": "attended-model", "provider": "attended-provider",
            "reasoning_effort": "high", "start_new_sessions": start_new_sessions,
        }},
        "agent": {"reasoning_effort": "low"},
    }
    _runtime(monkeypatch, runner, config)
    await _seed_caller(runner, home)
    source = _source(thread_id="6001")
    key = runner._session_key_for_source(source)
    assert await runner.async_session_store.lookup_by_session_key(key) is None

    result = await create_topic_session(runner, _source(), TopicSpec(name="Room", icon="💡"))

    expected = ("attended-model", "attended-provider", "high") if start_new_sessions else (
        "default-model", "default-provider", "low",
    )
    assert (result["effective_model"], result["effective_provider"], result["effective_effort"]) == expected
    entry = await runner.async_session_store.lookup_by_session_key(key)
    assert result["session_id"] == entry.session_id
    assert entry.created_at == entry.updated_at
    assert runner._peek_session_state(key) is None

    runner.hooks = SimpleNamespace(emit=AsyncMock())
    _, is_new = await runner._hmwa_open_session(entry, key, source)
    assert is_new
    model, runtime = await runner._resolve_session_agent_runtime_async(
        source=source, session_key=key, user_config=config,
    )
    reasoning = runner._resolve_session_reasoning_config(session_key=key, model=model, user_config=config)
    assert (model, runtime["provider"], reasoning["effort"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "edit"])
async def test_cold_topic_readback_keeps_gateway_loop_responsive(home, monkeypatch, operation):
    runner = _runner(home, FakeTelegram())
    _runtime(monkeypatch, runner, {"model": {"default": "default-model"}})
    await _seed_caller(runner, home, thread_id="4242")
    key = runner._session_key_for_source(_source(thread_id="4242"))
    runner.session_store.set_model_override(key, {"model": "session-model", "provider": "cold-provider"})
    entered, release = threading.Event(), threading.Event()
    yielded = []

    def credentials(provider, **_):
        entered.set()
        yielded.append(release.wait(5))
        return {"provider": provider, "api_key": "fixture"}

    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", credentials)
    if operation == "create":
        task = asyncio.create_task(create_topic_session(runner, _source(), TopicSpec(name="Room", icon="💡")))
    else:
        task = asyncio.create_task(edit_topic_session(runner, _source(), "4242", TopicSpec(name="Renamed")))
    try:
        assert await asyncio.to_thread(entered.wait, 10), "cold credentials were never resolved"
        release.set()
        result = await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert yielded and all(yielded), "credential lookup blocked the gateway loop"
    assert result["effective_model"] == "session-model"
    assert result["effective_provider"] == "cold-provider"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "edit"])
@pytest.mark.parametrize("failure", ["runtime", "reasoning", "role"])
async def test_readback_failure_preserves_successful_topic_operation(home, monkeypatch, operation, failure):
    adapter = FakeTelegram()
    runner = _runner(home, adapter)
    entry = await _seed_caller(runner, home, thread_id="4242")

    def unavailable(*_, **__):
        raise RuntimeError("provider unavailable")

    if failure == "runtime":
        monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", unavailable)
    elif failure == "reasoning":
        runner._load_reasoning_config = unavailable
    else:
        # Validation succeeds, then the role disappears before the result is built.
        resolve = runner._set_session_model_role

        async def set_role(session_key, role):
            await resolve(session_key, role)
            monkeypatch.setattr("gateway.telegram_topic_sessions._resolve_topic_model_ref", unavailable)

        runner._set_session_model_role = set_role
        monkeypatch.setattr("gateway.run._load_gateway_config", lambda *_: {
            "model": {"default": "base", "attended": {"model": "attended-model"}},
        })
    spec = TopicSpec(name="Room", icon="💡", message="start here", model="@attended" if failure == "role" else "")
    if operation == "create":
        result = await create_topic_session(runner, _source(), spec)
        assert result["brief_delivered"] is True
        assert len(adapter.created) == len(adapter.admitted) == 1
    else:
        result = await edit_topic_session(runner, _source(), "4242", spec)
        assert len(adapter.renamed) == 1
    assert result["action"] == operation
    assert result["thread_id"] == "4242"
    assert result["session_id"] == entry.session_id
    assert home.db.get_session_title(entry.session_id) == "Room"
    assert home.db.get_telegram_topic_binding(chat_id=_source().chat_id, thread_id="4242")["session_id"] == entry.session_id
    assert result["warnings"] and "readback" in result["warnings"][-1].lower()
    assert all(result[key] is None for key in ("effective_model", "effective_provider", "effective_effort"))


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["fallback", "temporary", "expired", "new_session", "cold", "deferred"])
async def test_readback_failure_preserves_turn_state(home, monkeypatch, scenario):
    from gateway.telegram_topic_sessions import _session_settings_readback

    runner = _runner(home, FakeTelegram())
    config = {"model": {"default": "default-model", "attended": {
        "model": "attended-model", "provider": "attended-provider",
    }, "idle_revert_minutes": 15}}
    _runtime(monkeypatch, runner, config)
    source = _source(thread_id="4242")
    entry = await _seed_caller(runner, home, thread_id="4242")
    key = entry.session_key
    # These are existing, idle conversations, not untouched first-turn sessions.
    await runner.async_session_store.mark_user_activity(key, when=datetime.fromtimestamp(time.time() - 86400))
    if scenario != "cold":
        conversation = runner._session_state(key).conversation
        conversation.pending_attended_notice = "already staged"
        conversation.pre_agent_fallback_notice = "session fallback"
        conversation.last_resolved_model = "previous-model"
        if scenario in {"temporary", "expired"}:
            deadline = time.time() + (43200 if scenario == "temporary" else -1)
            override = {"model": "picked-model", "provider": "pick-provider", "expires_at": deadline}
            await runner._persist_session_model_override(key, override, scope="temporary")
        if scenario == "new_session":
            conversation.attended_new_session = True
        if scenario == "deferred":
            from gateway.attended_model import resolve_attended_model
            conversation.last_user_activity_at = time.time() - 86400
            conversation.last_attended_decision = resolve_attended_model({
                "last_user_activity_at": time.time(), "default_model": "default-model",
            }, time.time(), config)
        if scenario == "fallback":
            monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
                "provider": "default-provider", "api_key": "fixture", "_fallback_notice": "new fallback",
            })
    runner._pre_agent_fallback_notice = "runner fallback"
    runner._pre_agent_fallback_notices = {key: "queued fallback"}
    from gateway.session_state import SERVICE_TIER_UNSET
    sessions_before = copy.deepcopy(
        runner.__dict__.get("_sessions", {}), {id(SERVICE_TIER_UNSET): SERVICE_TIER_UNSET},
    )
    notices_before = copy.deepcopy(runner._pre_agent_fallback_notices)
    stored_before = copy.deepcopy(runner.session_store.get_model_override(key))
    reasoning_before = runner.session_store.get_reasoning_override(key)

    result = await _session_settings_readback(runner, source, entry, None)

    assert runner.__dict__.get("_sessions", {}) == sessions_before
    assert runner._pre_agent_fallback_notice == "runner fallback"
    assert runner._pre_agent_fallback_notices == notices_before
    assert runner.session_store.get_model_override(key) == stored_before
    assert runner.session_store.get_reasoning_override(key) == reasoning_before
    expected = {"temporary": "picked-model", "new_session": "attended-model", "deferred": "attended-model"}.get(scenario, "default-model")
    assert result["effective_model"] == expected
    model, runtime = await runner._resolve_session_agent_runtime_async(
        source=source, session_key=key, user_config=config,
    )
    assert (result["effective_model"], result["effective_provider"]) == (model, runtime.get("provider"))
