from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore


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
    runner._set_session_reasoning_override = Mock(side_effect=lambda key, value, **_kwargs: (
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
    # The running session's persisted reasoning is cleared without touching its live agent.
    runner._set_session_reasoning_override.assert_called_once_with("running", None, scope="session")
    assert {c.args[0] for c in runner._set_reasoning_override.call_args_list} == {"idle-a", "reasoning-only"}
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

    adapter.config.extra = {}
    adapter._allow_from = {"owner"}
    assert GatewayModelCommandsMixin._model_reset_all_is_authorized(runner, source) is True

    group_source = SessionSource(platform=Platform.TELEGRAM, user_id="owner", chat_id="group", chat_type="group")
    assert GatewayModelCommandsMixin._model_reset_all_is_authorized(runner, group_source) is False


@pytest.mark.parametrize(
    "env,extra,user_id,chat_type,expected",
    [
        ({"TELEGRAM_ALLOWED_USERS": "owner"}, {}, "owner", "dm", True),
        ({"TELEGRAM_ALLOWED_USERS": "owner"}, {}, "stranger", "dm", False),
        ({"GATEWAY_ALLOWED_USERS": "owner"}, {}, "owner", "dm", True),
        ({"TELEGRAM_ALLOWED_USERS": "owner"}, {}, "owner", "group", False),
        ({"GATEWAY_ALLOW_ALL_USERS": "true"}, {}, "stranger", "dm", False),
        ({"TELEGRAM_ALLOW_ALL_USERS": "true"}, {}, "stranger", "dm", False),
        ({"TELEGRAM_ALLOWED_USERS": "*"}, {}, "stranger", "dm", False),
        ({"GATEWAY_ALLOWED_USERS": "*"}, {}, "stranger", "dm", False),
        ({}, {"allow_from": ["*"]}, "stranger", "dm", True),
        ({"TELEGRAM_ALLOWED_USERS": "owner"}, {"allow_admin_from": ["admin"]}, "owner", "dm", False),
        ({}, {"allow_admin_from": ["admin"]}, "admin", "dm", True),
    ],
)
def test_reset_all_dm_authorization_uses_shared_allowlist_without_promoting_open_access(
    monkeypatch, env, extra, user_id, chat_type, expected,
):
    for key in (
        "TELEGRAM_ALLOWED_USERS", "TELEGRAM_ALLOW_ALL_USERS", "TELEGRAM_ALLOW_BOTS",
        "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    runner = object.__new__(GatewayRunner)
    platform_config = PlatformConfig(extra=extra)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: platform_config})
    adapter = SimpleNamespace(config=platform_config)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda _source: adapter
    source = SessionSource(platform=Platform.TELEGRAM, user_id=user_id, chat_id=user_id, chat_type=chat_type)

    assert runner._model_reset_all_is_authorized(source) is expected


@pytest.mark.parametrize(
    "source_fields,adapter_fields,env,paired",
    [
        ({"role_authorized": True}, {}, {}, False),
        ({"delivered_via_upstream_relay": True}, {}, {}, False),
        ({}, {"authorization_is_upstream": True}, {}, False),
        ({"is_bot": True}, {}, {"DISCORD_ALLOW_BOTS": "all"}, False),
        ({"is_bot": True}, {}, {"DISCORD_ALLOW_BOTS": "mentions"}, False),
        ({"is_bot": True, "user_id": "owner"}, {}, {}, False),
        ({}, {}, {}, True),
    ],
    ids=["role", "relay", "upstream-adapter", "all-bots", "mention-bots", "owner-bot", "paired-user"],
)
def test_reset_all_does_not_promote_inbound_grants_to_owner(
    monkeypatch, source_fields, adapter_fields, env, paired,
):
    for key in (
        "DISCORD_ALLOWED_USERS", "DISCORD_ALLOW_ALL_USERS", "DISCORD_ALLOW_BOTS",
        "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "owner")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    runner = object.__new__(GatewayRunner)
    platform_config = PlatformConfig(extra={})
    runner.config = GatewayConfig(platforms={Platform.DISCORD: platform_config})
    adapter = SimpleNamespace(config=platform_config, **adapter_fields)
    monkeypatch.setattr(runner, "adapters", {Platform.DISCORD: adapter}, raising=False)
    monkeypatch.setattr(runner, "_delivery_adapter_for", lambda source: adapter)
    monkeypatch.setattr(runner, "_adapter_profile_for_source", lambda source: None)
    if paired:
        monkeypatch.setattr(runner, "pairing_store", SimpleNamespace(is_approved=lambda *_args: True), raising=False)
    fields = {"platform": Platform.DISCORD, "user_id": "member", "chat_id": "dm", "chat_type": "dm"}
    source = SessionSource(**(fields | source_fields))

    assert runner._is_user_authorized(source) is True
    assert runner._model_reset_all_is_authorized(source) is False


@pytest.mark.parametrize(
    "allow_key,expected",
    [("DISCORD_ALLOWED_USERS", True), ("GATEWAY_ALLOWED_USERS", False)],
)
def test_reset_all_owner_uses_only_platform_allowlist_resolutions(monkeypatch, allow_key, expected):
    for key in (
        "DISCORD_ALLOWED_USERS", "DISCORD_ALLOW_ALL_USERS", "DISCORD_ALLOW_BOTS",
        "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(allow_key, "owner-name")
    runner = object.__new__(GatewayRunner)
    platform_config = PlatformConfig(extra={})
    runner.config = GatewayConfig(platforms={Platform.DISCORD: platform_config})
    adapter = SimpleNamespace(config=platform_config, resolved_allowlist_user_ids=lambda: {"owner-id"})
    monkeypatch.setattr(runner, "adapters", {Platform.DISCORD: adapter}, raising=False)
    monkeypatch.setattr(runner, "_delivery_adapter_for", lambda source: adapter)
    source = SessionSource(platform=Platform.DISCORD, user_id="owner-id", chat_id="dm", chat_type="dm")

    assert runner._model_reset_all_is_authorized(source) is expected


@pytest.mark.asyncio
async def test_reset_all_clears_sqlite_routing_picks_after_restart(tmp_path, monkeypatch):
    import asyncio
    import json
    import sqlite3

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = GatewayConfig(write_sessions_json=False)
    store = SessionStore(tmp_path / "sessions", config)
    entries = {}
    for name in ("model-only", "reasoning-only", "running"):
        entry = store.get_or_create_session(
            SessionSource(platform=Platform.TELEGRAM, user_id="owner", chat_id=name, chat_type="dm")
        )
        entries[name] = entry
        if name != "reasoning-only":
            store.set_model_override(entry.session_key, {"model": "picked", "provider": "nous"})
        if name != "model-only":
            store.set_reasoning_override(entry.session_key, {"enabled": True, "effort": "high"})
    keys = {entry.session_key for entry in entries.values()}
    running_key = entries["running"].session_key
    assert store.override_session_keys() == keys
    with sqlite3.connect(tmp_path / "state.db") as db:
        before = [json.loads(row[0]) for row in db.execute("SELECT entry_json FROM gateway_routing")]
    assert any(row.get("model_override") for row in before)
    assert any(row.get("reasoning_override") for row in before)

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._evict_cached_agent = Mock()
    live_agent = SimpleNamespace(reasoning_config={"enabled": True, "effort": "high"})
    runner._running_agents = {running_key: live_agent}

    assert await runner._reset_all_session_overrides() == len(keys)
    assert await asyncio.to_thread(store.override_session_keys) == set()
    assert live_agent.reasoning_config == {"enabled": True, "effort": "high"}
    assert runner._bulk_reset_pending_sessions == {running_key}
    await asyncio.to_thread(store.close_all_db_handles)

    with sqlite3.connect(tmp_path / "state.db") as db:
        after = [json.loads(row[0]) for row in db.execute("SELECT entry_json FROM gateway_routing")]
    assert len(after) == len(keys)
    assert all(row.get("model_override") is None and row.get("reasoning_override") is None for row in after)
    restarted = SessionStore(tmp_path / "sessions", config)
    try:
        assert await asyncio.to_thread(restarted.override_session_keys) == set()
        for key in keys:
            assert await asyncio.to_thread(restarted.get_model_override, key) is None
            assert await asyncio.to_thread(restarted.get_reasoning_override, key) is None
    finally:
        await asyncio.to_thread(restarted.close_all_db_handles)


def test_turn_release_drains_pending_bulk_reset():
    runner, _states = _runner_for_bulk_reset({"running"}, running={"running"})
    runner._bulk_reset_pending_sessions = {"running"}
    runner._persist_active_agents = Mock()
    runner._is_session_run_current = lambda *_args: True
    turn = SimpleNamespace(lease=None, retired_agents=[], clear=Mock())
    runner._peek_session_state = lambda _key: SimpleNamespace(turn=turn)

    assert runner._release_running_agent_state("running") is True
    assert runner._bulk_reset_pending_sessions == set()
