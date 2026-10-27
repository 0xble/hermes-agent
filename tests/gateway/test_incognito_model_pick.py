"""Private intervals preserve the original wall-clock model-pick policy."""
import asyncio

import pytest

from agent.incognito import register_session
from gateway.platforms.event import MessageEvent
from tests.gateway.test_model_pick_expiry import harness as model_pick_harness


@pytest.fixture
def harness(model_pick_harness, monkeypatch):
    cfg = model_pick_harness[0]
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda config=None: (config or cfg)["model"]["default"])
    return model_pick_harness


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("restart_at", [101_000.0, 143_200.0])
def test_private_restart_preserves_timed_pick_deadline(harness, asynchronous, restart_at):
    cfg, clock, source, restart = harness
    cfg["model"]["pick_ttl_hours"] = 12
    runner = restart()
    key = runner._session_key_for_source(source)
    entry = runner.session_store.lookup_by_session_key(key)
    try:
        asyncio.run(runner._handle_model_command(MessageEvent(text="/model picked", source=source)))
        asyncio.run(runner._handle_incognito_command(MessageEvent(text="/incognito on", source=source)))
        saved = (runner.session_store.sessions_dir / "sessions.json").read_bytes()
        clock.now = restart_at
        runner = restart()
        entry = runner.session_store.lookup_by_session_key(key)
        assert entry.incognito
        assert entry.model_override_scope == "temporary"
        assert float(entry.model_override["expires_at"]) == 143_200.0

        def resolve():
            if asynchronous:
                return asyncio.run(runner._resolve_session_agent_runtime_async(source=source, user_config=cfg))[0]
            return runner._resolve_session_agent_runtime(source=source, user_config=cfg)[0]

        assert resolve() == ("picked" if restart_at < 143_200.0 else "default")
        clock.now = 143_200.0
        assert resolve() == "default"
        assert entry.incognito
        assert (runner.session_store.sessions_dir / "sessions.json").read_bytes() == saved
        asyncio.run(runner._handle_incognito_command(MessageEvent(text="/incognito off", source=source)))
        assert not entry.incognito
        assert float(entry.model_override["expires_at"]) == 143_200.0
        assert resolve() == "default"
    finally:
        register_session(entry.session_id, False)
        register_session(key, False)


@pytest.mark.parametrize("restart_private", [False, True])
def test_configured_private_route_uses_session_scope_and_restores_unrenewed_pick(harness, restart_private):
    cfg, clock, source, restart = harness
    cfg["model"]["pick_ttl_hours"] = 12
    runner = restart()
    key = runner._session_key_for_source(source)
    entry = runner.session_store.lookup_by_session_key(key)
    try:
        asyncio.run(runner._handle_model_command(MessageEvent(text="/model picked", source=source)))
        cfg["incognito"] = {"model": "private-route"}
        runner._perform_model_switch.return_value[0].new_model = "private-route"
        asyncio.run(runner._handle_incognito_command(MessageEvent(text="/incognito on", source=source)))
        if restart_private:
            runner = restart()
            entry = runner.session_store.lookup_by_session_key(key)
            runner._rehydrate_session_model_override(key)
        assert entry.model_override_scope == "session"
        assert runner._session_model_override_scope(key) == "session"
        clock.now = 143_200.0
        assert runner._resolve_session_agent_runtime(source=source, user_config=cfg)[0] == "private-route"
        assert not runner._session_state(key).conversation.model_pick_expired
        asyncio.run(runner._handle_incognito_command(MessageEvent(text="/incognito off", source=source)))
        assert entry.model_override_scope == "temporary"
        assert float(entry.model_override["expires_at"]) == 143_200.0
        assert runner._session_model_override_scope(key) == "temporary"
        assert not runner._session_state(key).conversation.model_pick_expired
        assert runner._resolve_session_agent_runtime(source=source, user_config=cfg)[0] == "default"
    finally:
        register_session(entry.session_id, False)
        register_session(key, False)
