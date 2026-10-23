"""Topic results distinguish the requested pick from the destination's runtime route."""

import asyncio
import threading
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
