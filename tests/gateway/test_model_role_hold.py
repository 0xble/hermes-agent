"""@role holds: persistence, reset, concrete-pick precedence, and cards."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.attended_model import resolve_attended_model
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, SessionStore
from gateway.session_state import ConversationState


def _source(chat_id="c1", *, chat_type="dm"):
    return SessionSource(
        platform=Platform.TELEGRAM, user_id="owner", chat_id=chat_id, chat_type=chat_type,
    )


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_state.SessionDB",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("disabled")),
    )
    return SessionStore(tmp_path, GatewayConfig())


def _event(text, source=None):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source or _source())


def _attended_cfg(**attended):
    return {
        "model": {
            "default": "gpt-6.1-sol",
            "provider": "nous",
            "attended": {"model": "claude-opus-5-5", "provider": "custom:claude", **attended},
        },
        "model_presets": {
            "fast": {"provider": "openrouter", "model": "fast-model", "reasoning_effort": "minimal"},
        },
    }


def _runner(store, *, config=None, states=None):
    runner = object.__new__(GatewayRunner)
    states = states if states is not None else {}
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._running_agents = {}
    runner._pending_one_turn_model_restores = {}
    runner._session_model_override_locks = {}
    runner.session_store = store
    runner._async_session_store = SimpleNamespace(_store=store, set_model_override=AsyncMock())
    runner._session_state = lambda key: states.setdefault(
        key, SimpleNamespace(conversation=ConversationState(), persistent=SimpleNamespace(model_override_epoch=0)),
    )
    runner._peek_session_state = lambda key: states.get(key)
    runner._evict_cached_agent = MagicMock()
    runner._set_reasoning_override = MagicMock()
    runner._set_session_reasoning_override = MagicMock()
    runner._persist_session_model_override = AsyncMock()
    runner._persist_session_model_override_locked = AsyncMock()
    return runner, states




def test_role_preset_reasoning_applies_when_attended_is_unconfigured(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    cfg = {
        "model": {"default": "gpt-6.1-sol", "provider": "nous"},
        "model_presets": {
            "fast": {"provider": "openrouter", "model": "fast-model", "reasoning_effort": "minimal"},
        },
    }
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda *a, **k: cfg)
    store = _store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(_source())
    store.set_model_role(entry.session_key, "fast")
    runner, _states = _runner(store)
    runner._session_reasoning_override = lambda _key: None
    runner._session_model_override = lambda _key: None
    # Turn start hydrates the persisted role into conversation state; the sync resolver reads that.
    runner._session_state(entry.session_key).conversation.model_role = store.get_model_role(entry.session_key)

    result = runner._resolve_session_reasoning_config(
        source=_source(), session_key=entry.session_key, model="gpt-6.1-sol",
    )

    assert result == {"enabled": True, "effort": "minimal"}


def test_conversation_clear_drops_model_role():
    conversation = ConversationState(model_role="attended", model_override={"model": "picked"})
    conversation.clear()
    assert conversation.model_role is None
    assert conversation.model_override is None


def test_session_entry_role_round_trips_and_resume_repoint_keeps_it(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(_source())
    store.set_model_role(entry.session_key, "attended")

    restored = SessionEntry.from_dict(store.get_or_create_session(_source()).to_dict())
    assert restored.model_role == "attended"

    repointed = store.switch_session(entry.session_key, entry.session_id + "-resumed")
    assert repointed.model_role == "attended"
    assert store.get_model_role(entry.session_key) == "attended"


@pytest.mark.asyncio
async def test_new_clears_persisted_role(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(_source())
    store.set_model_role(entry.session_key, "attended")
    runner, _states = _runner(store)

    await runner._clear_conversation_scope_async(entry.session_key, reason="session_reset")

    assert store.get_model_role(entry.session_key) is None


def test_idle_role_stays_on_the_current_attended_model():
    cfg = _attended_cfg()
    result = resolve_attended_model(
        {
            "default_model": "gpt-6.1-sol",
            "default_provider": "nous",
            "model_role": "attended",
            "last_user_activity_at": time.time() - 3600,
        },
        time.time(),
        cfg,
    )
    assert result.reason == "role"
    assert result.model == "claude-opus-5-5"
    assert result.role == "attended"


def test_attended_role_follows_a_config_edit_between_resolves():
    state = {
        "default_model": "gpt-6.1-sol",
        "model_role": "attended",
        "last_user_activity_at": time.time() - 3600,
    }
    first = resolve_attended_model(state, time.time(), _attended_cfg())
    second = resolve_attended_model(
        state, time.time(), _attended_cfg(model="claude-sonnet-next"),
    )
    assert first.model == "claude-opus-5-5"
    assert second.reason == "role"
    assert second.model == "claude-sonnet-next"


def test_custom_preset_role_follows_a_preset_edit():
    state = {"default_model": "gpt-6.1-sol", "model_role": "fast"}
    first = resolve_attended_model(state, time.time(), _attended_cfg())
    edited = _attended_cfg()
    edited["model_presets"]["fast"]["model"] = "faster-model"
    second = resolve_attended_model(state, time.time(), edited)
    assert first.model == "fast-model"
    assert second.model == "faster-model"
    assert second.reason == "role"


def test_unknown_role_falls_back_to_default_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="gateway.attended_model"):
        result = resolve_attended_model(
            {"default_model": "gpt-6.1-sol", "default_provider": "nous", "model_role": "missing"},
            time.time(),
            _attended_cfg(),
        )
    assert result.reason == "default"
    assert result.model == "gpt-6.1-sol"
    assert "fallback=default" in caplog.text


def test_explicit_model_beats_role_for_its_scope_and_leaves_the_role_stored():
    result = resolve_attended_model(
        {
            "default_model": "gpt-6.1-sol",
            "model_role": "attended",
            "once_model": "one-turn-model",
            "once_provider": "openrouter",
            "session_model": "session-model",
        },
        time.time(),
        _attended_cfg(),
    )
    assert result.reason == "explicit"
    assert result.model == "one-turn-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_global", [False, True], ids=["session", "global_or_promoted"])
async def test_concrete_model_clears_role_except_once(tmp_path, monkeypatch, persist_global):
    store = _store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(_source())
    store.set_model_role(entry.session_key, "attended")
    runner, states = _runner(store)
    runner._channel_override_for = lambda _source: None
    persisted = AsyncMock()
    monkeypatch.setattr("gateway.slash_commands_model._persist_model_switch_to_config", persisted)
    conversation = states.setdefault(
        entry.session_key,
        SimpleNamespace(
            conversation=ConversationState(model_role="attended"),
            persistent=SimpleNamespace(model_override_epoch=0),
        ),
    ).conversation
    runner._session_state = lambda key: states[key]
    ctx = SimpleNamespace(
        session_key=entry.session_key,
        persist_global=persist_global,
        config_path=tmp_path / "config.yaml",
        restore_snapshot=None,
        model_override_scope="session",
        pick_ttl_hours=0.0,
        current_model="old-model",
    )
    result = SimpleNamespace(
        new_model="gpt-5.5", target_provider="openrouter", api_key="sk", base_url="https://example.invalid",
        api_mode="chat_completions", request_overrides={}, runtime_capabilities={},
        provider_label="OpenRouter",
    )

    await runner._record_model_switch_locked(result, ctx, source=_source(), one_turn=True, picker=False)
    assert conversation.model_role == "attended"
    assert store.get_model_role(entry.session_key) == "attended"

    persisted.reset_mock()
    await runner._record_model_switch_locked(result, ctx, source=_source(), one_turn=False, picker=False)
    assert persisted.await_count == (1 if persist_global else 0)
    assert conversation.model_role is None
    assert store.get_model_role(entry.session_key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["--global", "--once", "--session", "--provider openrouter", "--reasoning high"])
async def test_model_role_rejects_flags(tmp_path, monkeypatch, flag):
    store = _store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(_source())
    runner, _states = _runner(store)
    monkeypatch.setattr("gateway.run._load_gateway_config", _attended_cfg)

    reply = await runner._handle_model_command_locked(_event(f"/model @fast {flag}"))

    assert reply.startswith("❌")
    assert "@role" in reply
    assert store.get_model_role(entry.session_key) is None


@pytest.mark.asyncio
async def test_model_role_alone_sets_hold(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(_source())
    runner, _states = _runner(store)
    monkeypatch.setattr("gateway.run._load_gateway_config", _attended_cfg)

    runner._perform_model_switch = AsyncMock(return_value=(SimpleNamespace(
        new_model="fast-model", target_provider="openrouter", model_info=None, api_key="", base_url="",
    ), None))
    runner._model_selection_warning = AsyncMock(return_value=None)
    reply = await runner._handle_model_command_locked(_event("/model @fast"))

    assert "@fast" in reply and reply.startswith("✓")
    assert store.get_model_role(entry.session_key) == "fast"


@pytest.mark.asyncio
async def test_session_reset_clears_role_when_attended_is_unconfigured(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(_source())
    store.set_model_role(entry.session_key, "attended")
    runner, states = _runner(store)
    states[entry.session_key] = SimpleNamespace(
        conversation=ConversationState(model_role="attended", model_override={"model": "picked"}),
        persistent=SimpleNamespace(model_override_epoch=0),
    )
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"model": {"default": "gpt-6.1-sol"}})
    monkeypatch.setattr(
        "hermes_cli.model_switch.parse_model_switch_args",
        lambda _raw: SimpleNamespace(
            errors=[], error_messages=list, force_refresh=False, target="reset",
            explicit_provider=None, reasoning_effort=None, is_all=False, is_global=False,
            is_session=False, is_once=False,
        ),
    )

    reply = await runner._handle_model_command_locked(_event("/model reset"))

    assert "reset" in reply.lower() or reply
    assert store.get_model_role(entry.session_key) is None
    assert states[entry.session_key].conversation.model_role is None
    runner._persist_session_model_override_locked.assert_awaited()


@pytest.mark.asyncio
async def test_reset_all_clears_roles_in_memory_and_store(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    role_only = store.get_or_create_session(_source("role-only")).session_key
    store.set_model_role(role_only, "attended")
    both = store.get_or_create_session(_source("both")).session_key
    store.set_model_override(both, {"model": "picked", "provider": "nous"})
    store.set_model_role(both, "fast")
    runner, states = _runner(store)
    for key in (role_only, both):
        states[key] = SimpleNamespace(
            conversation=ConversationState(model_role="attended"),
            persistent=SimpleNamespace(model_override_epoch=0),
        )
    runner._session_model_overrides = {both: {"model": "picked"}}

    count = await runner._reset_all_session_overrides()

    assert count == 2
    assert store.role_session_keys() == set()
    assert all(state.conversation.model_role is None for state in states.values())


@pytest.mark.asyncio
async def test_migration_returns_converted_keys_and_skips_roles():
    converted_key = "agent:main:telegram:dm:-100:42"
    already = "agent:main:telegram:dm:-100:7"
    discord = "agent:main:discord:dm:9"

    def entry(key, platform, *, model="picked", role=None):
        return SimpleNamespace(
            session_key=key,
            platform=SimpleNamespace(value=platform),
            model_override={"model": model} if model else None,
            model_role=role,
        )

    store = SimpleNamespace(
        list_sessions=lambda: [
            entry(converted_key, "telegram"),
            entry(already, "telegram", role="fast"),
            entry(discord, "discord"),
            entry("agent:main:telegram:dm:-100:empty", "telegram", model=""),
        ],
        set_model_role=MagicMock(),
    )
    runner, states = _runner(store)
    states[converted_key] = runner._session_state(converted_key)
    states[converted_key].conversation.model_override = {"model": "picked"}

    converted, failures = await runner._migrate_telegram_model_overrides_to_role("attended")

    assert converted == [converted_key]
    assert failures == []
    store.set_model_role.assert_called_once_with(converted_key, "attended", clear_overrides=True)
    assert states[converted_key].conversation.model_role == "attended"


@pytest.mark.asyncio
async def test_migration_applies_role_through_the_shared_setter():
    """The migration loop must use the shared role setter, including its in-memory pops."""
    key = "agent:main:telegram:dm:-100:42"
    store = SimpleNamespace(
        list_sessions=lambda: [SimpleNamespace(
            session_key=key, platform=SimpleNamespace(value="telegram"),
            model_override={"model": "picked"}, model_role=None,
        )],
        set_model_role=MagicMock(),
    )
    runner, states = _runner(store)
    states[key] = runner._session_state(key)
    states[key].conversation.model_override = {"model": "picked"}
    runner._session_model_overrides[key] = {"model": "picked"}
    runner._session_reasoning_overrides[key] = {"effort": "high"}
    calls = []

    async def shared(session_key, role):
        calls.append((session_key, role))
        await asyncio.to_thread(store.set_model_role, session_key, role, clear_overrides=True)
        states[session_key].conversation.model_role = role
        runner._session_model_overrides.pop(session_key, None)
        runner._session_reasoning_overrides.pop(session_key, None)
        runner._evict_cached_agent(session_key)

    runner._set_session_model_role = shared

    converted, failures = await runner._migrate_telegram_model_overrides_to_role("attended")

    assert converted == [key]
    assert failures == []
    assert calls == [(key, "attended")]
    assert key not in runner._session_model_overrides
    assert key not in runner._session_reasoning_overrides
    runner._evict_cached_agent.assert_called_once_with(key)


@pytest.mark.asyncio
async def test_migration_persists_every_conversion_in_one_routing_write():
    """``/model --all attended`` snapshots and persists the routing index once."""
    first = "agent:main:telegram:dm:-100:1"
    second = "agent:main:telegram:dm:-100:2"
    store = SimpleNamespace(
        list_sessions=lambda: [
            SimpleNamespace(session_key=first, platform=SimpleNamespace(value="telegram"),
                            model_override={"model": "picked-a"}, model_role=None),
            SimpleNamespace(session_key=second, platform=SimpleNamespace(value="telegram"),
                            model_override={"model": "picked-b"}, model_role=None),
        ],
        set_model_roles=MagicMock(),
        set_model_role=MagicMock(side_effect=AssertionError("per-session persist")),
    )
    runner, states = _runner(store)
    states[first] = runner._session_state(first)
    states[first].conversation.model_override = {"model": "picked-a"}
    states[second] = runner._session_state(second)

    converted, failures = await runner._migrate_telegram_model_overrides_to_role("attended")

    assert converted == [first, second]
    assert failures == []
    store.set_model_roles.assert_called_once_with([first, second], "attended", clear_overrides=True)
    assert states[first].conversation.model_role == "attended"
    assert states[first].conversation.model_override is None
    assert states[second].conversation.model_role == "attended"


def test_status_card_shows_role_marker():
    from gateway.slash_commands_status import GatewayStatusCommandsMixin

    entry = SessionEntry(
        session_key="k", session_id="s", created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC), model_role="attended",
    )
    runner = object.__new__(GatewayStatusCommandsMixin)
    runner._running_agents = {}
    runner.adapters = {}
    runner.session_store = SimpleNamespace(get_model_role=lambda _key: None)
    runner._queue_depth = lambda *_args, **_kwargs: 0
    runner._status_session_db_facts = AsyncMock(return_value=(None, {}, 0, {}))
    runner._cached_agent_for = lambda _key: None
    runner._rehydrate_session_model_override_async = AsyncMock(return_value=None)
    runner._session_model_override = lambda _key: {}
    runner._resolve_route_context = AsyncMock(return_value=None)
    runner.async_session_store = SimpleNamespace(get_or_create_session=AsyncMock(return_value=entry))

    card = asyncio.run(runner._handle_status_command(_event("/status")))

    assert "(@attended)" in card


def test_held_wakeup_posts_no_switch_card(monkeypatch):
    from gateway.run_turn import GatewayTurnMixin

    cfg = _attended_cfg()
    conversation = ConversationState(
        model_role="attended",
        last_user_activity_at=time.time() - 3600,
        pending_attended_notice=None,
        last_resolved_model="gpt-6.1-sol",
    )
    state = SimpleNamespace(conversation=conversation)
    runner = SimpleNamespace(
        _session_state=lambda _key: state,
        _peek_session_state=lambda _key: state,
        _load_attended_activity_stamp=lambda _key, **_kwargs: conversation.last_user_activity_at,
        _load_attended_route_active=lambda _key: None,
        _handoff_attended_state=lambda _key, conversation=None, runner=None: [
            setattr(state.conversation, name, value) for name, value in (conversation or {}).items()
        ],
        _session_reasoning_override=lambda _key: None,
        _load_reasoning_config=lambda _model: {},
        _ensure_attended_new_session_activity=lambda *args, **kwargs: (None, False),
        session_store=SimpleNamespace(get_model_role=lambda _key: "attended"),
    )
    runner._attended_resolution_for_session = lambda **kwargs: (
        GatewayTurnMixin._attended_resolution_for_session(runner, **kwargs)
    )
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda *_args, **_kwargs: "gpt-6.1-sol")

    route = GatewayTurnMixin._attended_route_for_session(
        runner, source=None, session_key="k", user_config=cfg, override=None, last_model="gpt-6.1-sol",
    )

    assert route["role"] == "attended"
    assert route["model"] == "claude-opus-5-5"
    assert conversation.pending_attended_notice is None


def test_per_turn_fallback_does_not_change_stored_role(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(_source())
    store.set_model_role(entry.session_key, "attended")
    from gateway.run_turn_runner import TurnRunner

    decision = SimpleNamespace(
        model="fallback-model", provider="openrouter", reasoning_effort="medium",
        reason="role", active=True, decision_key=("fallback-model", "openrouter", "medium", "role", True),
        switched_back=False, new_session=False,
    )
    runner = SimpleNamespace(
        _attended_resolution_for_session=lambda **_kwargs: decision,
        _session_model_override=lambda _key: None,
        _resolve_session_agent_runtime=lambda **_kwargs: ("fallback-model", {"provider": "openrouter", "api_mode": "chat_completions"}),
        _session_state=lambda _key: SimpleNamespace(conversation=ConversationState(model_role="attended")),
        session_store=store,
    )
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = SimpleNamespace(session_key=entry.session_key, source=None, user_config=_attended_cfg())
    turn.attended_notices = []
    turn._emit_persistent_attended_notice = turn.attended_notices.append
    agent = SimpleNamespace(
        model="primary", provider="nous", api_mode="chat_completions",
        _gateway_attended_decision_key=None, _fallback_activated=True, _provider_fallback_active=False,
        is_subagent=False, _is_auxiliary=False,
    )

    with patch("agent.agent_runtime_helpers.switch_model") as switch:
        turn._apply_attended_before_api_call(agent)

    switch.assert_not_called()
    assert store.get_model_role(entry.session_key) == "attended"
    assert turn.attended_notices == []


def test_role_activity_older_than_idle_window_is_not_a_switch_back():
    stamp = datetime.now(UTC) - timedelta(hours=2)
    result = resolve_attended_model(
        {
            "default_model": "gpt-6.1-sol",
            "model_role": "attended",
            "last_user_activity_at": stamp,
        },
        time.time(),
        _attended_cfg(),
    )
    assert result.reason == "role"
    assert result.switched_back is False


def test_attended_migration_reply_is_localized_with_counts_and_failures():
    from agent.i18n import t

    text = t("gateway.model.attended_migrated", converted=2, failures=1, detail=" (boom)")
    assert "2" in text and "1" in text and "(boom)" in text
    assert text != "gateway.model.attended_migrated"


def test_role_branch_session_reasoning_pick_wins_over_role_effort():
    cfg = _attended_cfg()
    state = {"model_role": "fast", "session_reasoning": "high", "default_reasoning": "low"}
    decision = resolve_attended_model(state, time.time(), cfg)
    assert decision.reason == "role" and decision.model == "fast-model"
    assert decision.reasoning_effort == "high"
    state.pop("session_reasoning")
    assert resolve_attended_model(state, time.time(), cfg).reasoning_effort == "minimal"


@pytest.mark.asyncio
async def test_model_command_with_literal_at_model_id_is_a_normal_pick_not_a_role(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(_source())
    runner, _states = _runner(store)
    monkeypatch.setattr("gateway.run._load_gateway_config", _attended_cfg)
    calls = []
    runner._set_session_model_role = AsyncMock(side_effect=lambda *a, **k: calls.append(a))

    with suppress_model_pick_failure():
        await runner._handle_model_command_locked(_event("/model @cf/meta/llama-3.3-70b-instruct-fp8-fast"))

    # A normal pick never sets a role (it may clear one).
    assert all(call[1] is None for call in calls)
    assert store.get_model_role(entry.session_key) is None


def suppress_model_pick_failure():
    import contextlib
    # The pick itself needs live provider resolution; only the role/no-role routing is under test.
    return contextlib.suppress(Exception)


@pytest.mark.asyncio
async def test_status_and_turn_read_role_from_conversation_without_touching_the_store(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    entry = store.get_or_create_session(_source())
    store.set_model_role(entry.session_key, "fast")
    conversation = ConversationState()
    state = SimpleNamespace(conversation=conversation)
    loop_thread = __import__("threading").get_ident()
    seen = []
    real_get = store.get_model_role
    store.get_model_role = lambda key: (seen.append(__import__("threading").get_ident()), real_get(key))[1]
    # Hydration (the only store read) runs off the event loop thread.
    conversation.model_role = await asyncio.to_thread(store.get_model_role, entry.session_key)
    assert seen and seen[0] != loop_thread
    seen.clear()
    from gateway.run_turn import GatewayTurnMixin
    runner = SimpleNamespace(
        _session_state=lambda _key: state, _peek_session_state=lambda _key: state,
        _load_attended_activity_stamp=lambda _key, **_kwargs: None,
        _handoff_attended_state=lambda *a, **k: None,
        _session_reasoning_override=lambda _key: None,
        _load_reasoning_config=lambda _model: {},
        _ensure_attended_new_session_activity=lambda *a, **k: (None, False),
        session_store=store,
    )
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda *a, **k: "gpt-6.1-sol")
    GatewayTurnMixin._attended_resolution_for_session(
        runner, source=None, session_key=entry.session_key, user_config=_attended_cfg(),
    )
    assert seen == []
