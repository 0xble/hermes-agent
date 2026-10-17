from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore


def _runner_for_bulk_reset(keys, *, running=()):
    runner = object.__new__(GatewayRunner)
    states = {
        key: SimpleNamespace(
            conversation=SimpleNamespace(
                model_override={"model": f"model-{key}"} if key != "reasoning-only" else None,
                reasoning_override={"enabled": True, "effort": "high"},
                model_override_scope="session",
                reasoning_override_scope="session",
            ),
            persistent=SimpleNamespace(model_override_epoch=0),
        )
        for key in keys
    }
    runner._session_model_overrides = {
        key: state.conversation.model_override
        for key, state in states.items()
        if state.conversation.model_override is not None
    }
    runner._session_reasoning_overrides = {
        key: state.conversation.reasoning_override for key, state in states.items()
    }
    runner._running_agents = {key: object() for key in running}
    runner._session_model_override_locks = {}
    runner._session_state = lambda session_key: states[session_key]
    runner._peek_session_state = lambda session_key: states.get(session_key)
    runner.session_store = SimpleNamespace(override_session_keys=lambda: set(keys))
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, set_model_override=AsyncMock()
    )
    runner._set_reasoning_override = Mock(side_effect=lambda key, value, **_kwargs: (
        setattr(states[key].conversation, "reasoning_override", value),
        runner._session_reasoning_overrides.pop(key, None),
    ))
    runner._evict_cached_agent = Mock()
    return runner, states


@pytest.mark.asyncio
async def test_reset_all_clears_memory_and_persistence_and_defers_running_route():
    runner, states = _runner_for_bulk_reset({"idle-a", "reasoning-only", "running"}, running={"running"})

    count = await runner._reset_all_session_overrides()

    assert count == 3
    assert runner.async_session_store.set_model_override.await_count == 3
    assert runner._evict_cached_agent.call_count == 2
    assert runner._bulk_reset_pending_sessions == {"running"}
    assert all(state.conversation.model_override is None for state in states.values())
    assert all(state.conversation.reasoning_override is None for state in states.values())

    runner._running_agents = {}
    assert runner._session_model_override("running") is None
    assert runner._bulk_reset_pending_sessions == set()


def test_session_store_clear_survives_json_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_state.SessionDB", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("disabled")))
    store = SessionStore(tmp_path, GatewayConfig())
    entry = store.get_or_create_session(
        SessionSource(platform=Platform.TELEGRAM, user_id="u", chat_id="c", chat_type="dm")
    )
    store.set_model_override(entry.session_key, {"model": "picked", "provider": "nous"})
    store.set_reasoning_override(entry.session_key, {"enabled": True, "effort": "high"})
    assert store.override_session_keys() == {entry.session_key}

    store.set_model_override(entry.session_key, None)
    store.set_reasoning_override(entry.session_key, None)
    restarted = SessionStore(tmp_path, GatewayConfig())
    assert restarted.override_session_keys() == set()
    assert restarted.get_model_override(entry.session_key) is None
    assert restarted.get_reasoning_override(entry.session_key) is None


def test_reset_all_dm_fallback_uses_adapter_config_extra_allowlist(monkeypatch):
    from gateway.slash_access import _DISABLED_POLICY
    from gateway.slash_commands_model import GatewayModelCommandsMixin

    runner = object.__new__(GatewayRunner)
    source = SessionSource(platform=Platform.TELEGRAM, user_id="owner", chat_id="owner", chat_type="dm")
    adapter = SimpleNamespace(config=SimpleNamespace(extra={"allow_from": ["owner"]}))
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    runner._adapter_extra_allowlist_authorizes = GatewayRunner._adapter_extra_allowlist_authorizes.__get__(runner)
    monkeypatch.setattr("gateway.slash_access.policy_for_runner_source", lambda *_args: _DISABLED_POLICY)

    assert GatewayModelCommandsMixin._model_reset_all_is_authorized(runner, source) is True

    group_source = SessionSource(platform=Platform.TELEGRAM, user_id="owner", chat_id="group", chat_type="group")
    assert GatewayModelCommandsMixin._model_reset_all_is_authorized(runner, group_source) is False


def test_reset_all_dm_fallback_uses_plugin_allowed_users_and_denies_other_user(monkeypatch):
    from gateway.slash_access import _DISABLED_POLICY
    from gateway.slash_commands_model import GatewayModelCommandsMixin

    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(config=SimpleNamespace(extra={"allowed_users": "owner,other"}))
    runner._delivery_adapter_for = lambda source: adapter
    runner._adapter_extra_allowlist_authorizes = GatewayRunner._adapter_extra_allowlist_authorizes.__get__(runner)
    monkeypatch.setattr("gateway.slash_access.policy_for_runner_source", lambda *_args: _DISABLED_POLICY)

    owner = SessionSource(platform=Platform.TELEGRAM, user_id="owner", chat_id="owner", chat_type="dm")
    stranger = SessionSource(platform=Platform.TELEGRAM, user_id="stranger", chat_id="stranger", chat_type="dm")
    assert GatewayModelCommandsMixin._model_reset_all_is_authorized(runner, owner) is True
    assert GatewayModelCommandsMixin._model_reset_all_is_authorized(runner, stranger) is False
