"""Tests for gateway /fast support and Priority Processing routing."""

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import hermes_yaml as yaml

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


class _CapturingAgent:
    last_init = None
    last_run = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(
        self,
        user_message,
        conversation_history=None,
        task_id=None,
        persist_user_message=None,
        persist_user_timestamp=None,
    ):
        type(self).last_run = {
            "user_message": user_message,
            "conversation_history": conversation_history,
            "task_id": task_id,
            "persist_user_message": persist_user_message,
            "persist_user_timestamp": persist_user_timestamp,
        }
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


def _install_fake_agent(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )


def _make_discord_auto_thread_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="999",
        chat_type="thread",
        user_id="user-1",
        thread_id="999",
        parent_chat_id="100",
        auto_thread_created=True,
        auto_thread_initial_name="raw user prompt",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def test_turn_route_injects_priority_processing_without_changing_runtime():
    runner = _make_runner()
    runner._service_tier = "priority"
    runtime_kwargs = {
        "api_key": "***",
        "base_url": "https://api.openai.com/v1",
        "provider": "openai",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }

    route = gateway_run.GatewayRunner._resolve_turn_agent_config(runner, "hi", "gpt-5.4", runtime_kwargs)

    assert route["runtime"]["provider"] == "openai"
    assert route["runtime"]["api_mode"] == "chat_completions"
    assert route["request_overrides"] == {"service_tier": "priority"}

    # Proxied routes never receive the param (OpenRouter strips it / others 400).
    runtime_kwargs.update(base_url="https://openrouter.ai/api/v1", provider="openrouter")
    route = gateway_run.GatewayRunner._resolve_turn_agent_config(runner, "hi", "gpt-5.4", runtime_kwargs)
    assert route["request_overrides"] == {}


@pytest.mark.asyncio
async def test_handle_fast_command_global_flag_persists_config(monkeypatch, tmp_path):
    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    # /fast now resolves eligibility through the session runtime resolver; with
    # no session override that path calls the real provider resolver, so stub it.
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {})

    response = await runner._handle_fast_command(_make_event("/fast fast --global"))

    assert "FAST" in response
    assert runner._service_tier == "priority"

    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["agent"]["service_tier"] == "fast"
    # Global write supersedes the session override.
    assert not runner._session_service_tier_overrides


@pytest.mark.asyncio
async def test_session_fast_override_beats_config_default(monkeypatch, tmp_path):
    """A session /fast normal wins over agent.service_tier: fast in config."""
    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"agent": {"service_tier": "fast"}},
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    # Eligibility routes through the session runtime resolver; stub the real
    # provider resolution the no-override path would otherwise trigger.
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {})

    event = _make_event("/fast normal")
    session_key = runner._session_key_for_source(event.source)

    response = await runner._handle_fast_command(event)

    assert "NORMAL" in response
    # Override stores explicit None (normal) and wins over config "fast".
    assert session_key in runner._session_service_tier_overrides
    assert runner._resolve_session_service_tier(session_key=session_key) is None
    # A different session still gets the config default.
    assert runner._resolve_session_service_tier(session_key="other-session") == "priority"


def _set_gateway_fast_config(monkeypatch, tmp_path, *, service_tier="", expiry=0):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"agent": {"service_tier": service_tier, "fast_expiry_seconds": expiry}},
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-6-astra")


@pytest.mark.asyncio
@pytest.mark.parametrize("default_tier", ["", "auto", "fast"])
async def test_session_fast_override_expires_to_explicit_normal(monkeypatch, tmp_path, default_tier):
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, service_tier=default_tier, expiry=10)
    clock = {"now": 100.0}
    monkeypatch.setattr("gateway.run_config_loaders.time.time", lambda: clock["now"])
    event = _make_event("/fast fast")
    session_key = runner._session_key_for_source(event.source)
    await runner._handle_fast_command(event)
    clock["now"] = 109.99
    assert runner._resolve_session_service_tier(session_key=session_key) == "priority"
    clock["now"] = 110.0
    tier, notice = runner._resolve_session_service_tier(session_key=session_key, report_transition=True)
    assert tier is None
    assert notice == "⚡ Fast mode switched off after 10s."
    state = runner._peek_session_state(session_key)
    assert state.conversation.service_tier_override is None
    assert state.conversation.service_tier_override_expires_at == 0
    tier, notice = runner._resolve_session_service_tier(session_key=session_key, report_transition=True)
    assert tier is None
    assert notice is None


@pytest.mark.asyncio
async def test_fast_expiry_default_zero_never_expires(monkeypatch, tmp_path):
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, expiry=0)
    clock = {"now": 100.0}
    monkeypatch.setattr("gateway.run_config_loaders.time.time", lambda: clock["now"])
    event = _make_event("/fast fast")
    session_key = runner._session_key_for_source(event.source)
    await runner._handle_fast_command(event)
    clock["now"] = 10_000.0
    assert runner._resolve_session_service_tier(session_key=session_key, report_transition=True) == ("priority", None)


@pytest.mark.asyncio
async def test_fast_reenable_restarts_expiry_clock_and_ultrafast_expires(monkeypatch, tmp_path):
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, expiry=10)
    clock = {"now": 100.0}
    monkeypatch.setattr("gateway.run_config_loaders.time.time", lambda: clock["now"])
    event = _make_event("/fast ultrafast")
    session_key = runner._session_key_for_source(event.source)
    await runner._handle_fast_command(event)
    clock["now"] = 109.0
    await runner._handle_fast_command(event)
    clock["now"] = 118.0
    assert runner._resolve_session_service_tier(session_key=session_key) == "ultrafast"
    clock["now"] = 119.0
    assert runner._resolve_session_service_tier(session_key=session_key) is None


@pytest.mark.asyncio
async def test_fast_global_never_expires(monkeypatch, tmp_path):
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, service_tier="fast", expiry=10)
    clock = {"now": 100.0}
    monkeypatch.setattr("gateway.run_config_loaders.time.time", lambda: clock["now"])
    session_key = runner._session_key_for_source(_make_event("/fast fast").source)
    await runner._handle_fast_command(_make_event("/fast fast --global"))
    clock["now"] = 10_000.0
    assert runner._resolve_session_service_tier(session_key=session_key, report_transition=True) == ("priority", None)


@pytest.mark.asyncio
async def test_fast_status_reports_remaining_time_in_hours_and_minutes(monkeypatch, tmp_path):
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, expiry=8 * 60 * 60)
    clock = {"now": 100.0}
    monkeypatch.setattr("gateway.run_config_loaders.time.time", lambda: clock["now"])
    await runner._handle_fast_command(_make_event("/fast fast"))
    clock["now"] = 101.2
    status = await runner._handle_fast_command(_make_event("/fast status"))
    assert "Expires in: 7h 59m" in status
    assert "7s" not in status


@pytest.mark.asyncio
async def test_live_agent_read_time_filter_is_non_mutating_until_resolution(monkeypatch, tmp_path):
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, expiry=10)
    clock = {"now": 100.0}
    monkeypatch.setattr("gateway.run_config_loaders.time.time", lambda: clock["now"])
    key = runner._session_key_for_source(_make_event("/fast fast").source)
    agent = SimpleNamespace(
        model="gpt-6-astra", provider="openai", base_url="https://api.openai.com/v1",
        request_overrides={"extra_body": {"keep": True}}, service_tier=None,
    )
    runner._running_agents[key] = agent
    await runner._handle_fast_command(_make_event("/fast fast"))
    assert agent.request_overrides["service_tier"] == "priority"
    clock["now"] = 111.0
    from agent.fast_mode import effective_request_overrides
    before = dict(agent.request_overrides)
    assert effective_request_overrides(agent) == {"extra_body": {"keep": True}}
    assert agent.request_overrides == before
    tier, notice = runner._resolve_session_service_tier(session_key=key, report_transition=True)
    assert (tier, notice) == (None, "⚡ Fast mode switched off after 10s.")
    assert agent.service_tier is None
    assert agent.request_overrides == {"extra_body": {"keep": True}}


@pytest.mark.asyncio
async def test_prepare_turn_stages_expiry_notice_once_before_run_sync(monkeypatch, tmp_path):
    """The real prepare-turn path resolves expiry immediately before staging sidecar notes."""
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, expiry=10)
    clock = {"now": 100.0}
    monkeypatch.setattr("gateway.run_config_loaders.time.time", lambda: clock["now"])
    event = _make_event("hello")
    source = event.source
    session_key = runner._session_key_for_source(source)
    await runner._handle_fast_command(_make_event("/fast fast"))
    clock["now"] = 111.0

    session_entry = SimpleNamespace(
        session_key=session_key, session_id="session-1", created_at=100.0, updated_at=100.0,
        yolo=False,
    )
    runner.config = SimpleNamespace(
        get_connected_platforms=lambda: [],
        get_home_channel=lambda _platform: None,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    runner._hmwa_open_session = AsyncMock(return_value=(False, False))
    runner._set_session_env = lambda _context: []
    runner._pinned_session_context_prompt = lambda *args, **kwargs: ""
    runner._hmwa_acquire_turn_lease = AsyncMock()
    runner._mark_durable_active_turn = AsyncMock()
    runner.session_store = SimpleNamespace(load_transcript=lambda _session_id: [])
    runner._hmwa_run_session_hygiene = AsyncMock(return_value=[])
    runner._hmwa_first_contact_notes = AsyncMock()
    runner._voice_channel_sidecar_note = lambda *args: None
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="hello")
    runner._hmwa_apply_message_timestamp = lambda _event, text: (text, None, None)
    runner._delivery_adapter_for = lambda _source: None
    runner._bind_adapter_run_generation = lambda *args: None

    # Open-session's second value is the session entry in production.
    runner._hmwa_open_session = AsyncMock(return_value=(False, session_entry))
    # Production order: handle_message claims the turn with the pending sentinel before preparing,
    # and an idle agent from the previous turn is still cached.
    runner._running_agents[session_key] = gateway_run._AGENT_PENDING_SENTINEL
    cached_agent = SimpleNamespace(request_overrides={"service_tier": "priority"}, service_tier="priority")
    runner._agent_cache[session_key] = (cached_agent, "sig", 0)
    prepared, _ = await runner._hmwa_prepare_turn(
        event, source, session_entry, session_key, "quick", 1,
    )

    assert isinstance(prepared, runner._PreparedTurn)
    assert runner._consume_pending_turn_sidecar_notes(session_key) == [
        "⚡ Fast mode switched off after 10s.",
    ]
    assert runner._consume_pending_turn_sidecar_notes(session_key) == []
    # Expiry behaved like /fast off: the stale cached agent is gone and the claim is untouched.
    assert session_key not in runner._agent_cache
    assert runner._running_agents[session_key] is gateway_run._AGENT_PENDING_SENTINEL
    assert not hasattr(gateway_run._AGENT_PENDING_SENTINEL, "_gateway_base_request_overrides")


@pytest.mark.asyncio
async def test_first_turn_after_expiry_gets_one_notice(monkeypatch, tmp_path):
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, expiry=10)
    clock = {"now": 100.0}
    monkeypatch.setattr("gateway.run_config_loaders.time.time", lambda: clock["now"])
    event = _make_event("/fast fast")
    session_key = runner._session_key_for_source(event.source)
    await runner._handle_fast_command(event)
    clock["now"] = 111.0
    tier, notice = runner._resolve_session_service_tier(session_key=session_key, report_transition=True)
    assert tier is None
    assert notice == "⚡ Fast mode switched off after 10s."
    runner._set_pending_turn_sidecar_notes(session_key, [notice])
    assert runner._consume_pending_turn_sidecar_notes(session_key) == [notice]
    assert runner._resolve_session_service_tier(session_key=session_key, report_transition=True) == (None, None)
    assert runner._consume_pending_turn_sidecar_notes(session_key) == []


@pytest.mark.asyncio
async def test_fast_selection_after_expiry_replies_once_and_restarts_deadline(monkeypatch, tmp_path):
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, expiry=10)
    clock = {"now": 100.0}
    monkeypatch.setattr("gateway.run_config_loaders.time.time", lambda: clock["now"])
    event = _make_event("/fast fast")
    session_key = runner._session_key_for_source(event.source)
    await runner._handle_fast_command(event)
    clock["now"] = 111.0
    reply = await runner._handle_fast_command(event)
    assert reply.count("Fast mode switched off") == 1
    assert runner._resolve_session_service_tier(session_key=session_key) == "priority"
    clock["now"] = 120.0
    assert runner._resolve_session_service_tier(session_key=session_key, report_transition=True) == ("priority", None)


@pytest.mark.asyncio
async def test_fast_status_after_expiry_shows_normal_and_notice_once(monkeypatch, tmp_path):
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, expiry=10)
    clock = {"now": 100.0}
    monkeypatch.setattr("gateway.run_config_loaders.time.time", lambda: clock["now"])
    event = _make_event("/fast fast")
    session_key = runner._session_key_for_source(event.source)
    await runner._handle_fast_command(event)
    clock["now"] = 111.0
    reply = await runner._handle_fast_command(_make_event("/fast status"))
    assert "normal" in reply.lower()
    assert reply.count("Fast mode switched off") == 1
    assert runner._resolve_session_service_tier(session_key=session_key, report_transition=True) == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["not-a-number", "", None, -1, "inf", "nan", True, False])
async def test_invalid_fast_expiry_is_disabled_with_warning(monkeypatch, tmp_path, caplog, invalid):
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, expiry=invalid)
    caplog.set_level("WARNING", logger="gateway.run")
    await runner._handle_fast_command(_make_event("/fast fast"))
    state = runner._peek_session_state(runner._session_key_for_source(_make_event("/fast fast").source))
    assert state.conversation.service_tier_override_expires_at == 0
    assert "fast_expiry_seconds" in caplog.text


@pytest.mark.asyncio
async def test_fast_reenable_clears_expiry_notice_before_next_turn(monkeypatch, tmp_path):
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, expiry=10)
    clock = {"now": 100.0}
    monkeypatch.setattr("gateway.run_config_loaders.time.time", lambda: clock["now"])
    event = _make_event("/fast fast")
    session_key = runner._session_key_for_source(event.source)
    await runner._handle_fast_command(event)
    clock["now"] = 111.0
    tier, notice = runner._resolve_session_service_tier(session_key=session_key, report_transition=True)
    assert tier is None
    assert notice == "⚡ Fast mode switched off after 10s."

    await runner._handle_fast_command(event)
    assert runner._resolve_session_service_tier(session_key=session_key, report_transition=True) == ("priority", None)


@pytest.mark.asyncio
async def test_expiry_notice_and_other_staged_note_are_delivered_once(monkeypatch, tmp_path):
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, expiry=10)
    clock = {"now": 100.0}
    monkeypatch.setattr("gateway.run_config_loaders.time.time", lambda: clock["now"])
    event = _make_event("/fast fast")
    session_key = runner._session_key_for_source(event.source)
    await runner._handle_fast_command(event)
    clock["now"] = 111.0
    tier, expiry_notice = runner._resolve_session_service_tier(session_key=session_key, report_transition=True)
    assert tier is None
    runner._set_pending_turn_sidecar_notes(session_key, [expiry_notice, "[Other note]"])
    assert runner._consume_pending_turn_sidecar_notes(session_key) == [
        "⚡ Fast mode switched off after 10s.", "[Other note]",
    ]
    assert runner._consume_pending_turn_sidecar_notes(session_key) == []


@pytest.mark.parametrize("tier", ["auto", "cold", "normal"])
def test_non_static_fast_modes_never_get_a_deadline(monkeypatch, tmp_path, tier):
    runner = _make_runner()
    _set_gateway_fast_config(monkeypatch, tmp_path, expiry=10)
    runner._set_session_service_tier_override("sk", {"normal": None}.get(tier, tier))
    state = runner._peek_session_state("sk")
    assert state.conversation.service_tier_override_expires_at == 0



@pytest.mark.asyncio
@pytest.mark.parametrize("base_url, provider, route_supported", [
    ("http://127.0.0.1:8317/v1", "custom:codex-proxy", False),  # proxy: params never sent
    ("https://api.openai.com/v1", "openai", True),              # first-party: fast applies
])
async def test_fast_follows_the_route_capability_gate(monkeypatch, tmp_path, route_supported, base_url, provider):
    """`/fast` must refuse a route that cannot carry the selected fast-mode parameters."""
    runner = _make_runner()
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    runner._resolve_session_agent_runtime = lambda **_: ("gpt-5.4", {"provider": provider, "base_url": base_url})

    response = await runner._handle_fast_command(_make_event("/fast fast"))
    assert response is not None
    if route_supported:
        assert "FAST" in response
    else:
        assert "only available" in response
        assert runner._resolve_session_service_tier(
            session_key=runner._session_key_for_source(_make_event("/fast fast").source)
        ) is None

    off = await runner._handle_fast_command(_make_event("/fast normal"))
    assert "no effect" not in off
_ASTRA_ON_CODEX = {"model": "gpt-6-astra", "provider": "openai-codex",
                   "base_url": "https://chatgpt.com/backend-api/codex", "api_key": "***"}
_GPT_ON_OPENROUTER = {"model": "openai/gpt-5.4", "provider": "openrouter",
                      "base_url": "https://openrouter.ai/api/v1", "api_key": "***"}


@pytest.mark.asyncio
@pytest.mark.parametrize("default_model, override, command, accepted", [
    # #118761: Astra picked with session /model over a default /fast can't serve.
    ("claude-sonnet-4-6", _ASTRA_ON_CODEX, "/fast ultrafast", True),
    # Converse: a fast-capable default must not admit a session route whose turns never carry the tier.
    ("claude-opus-5-5", _GPT_ON_OPENROUTER, "/fast fast", False),
])
async def test_fast_gate_follows_the_session_route(monkeypatch, tmp_path, default_model, override, command, accepted):
    """Real fast-mode tables: /fast accepts exactly the tiers the session's next turn would send."""
    runner = _make_runner()
    event = _make_event(command)
    session_key = runner._session_key_for_source(event.source)
    runner._session_model_overrides[session_key] = dict(override)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: default_model)

    response = await runner._handle_fast_command(event)

    tier = runner._resolve_session_service_tier(session_key=session_key)
    model, runtime = runner._resolve_session_agent_runtime(source=event.source)
    route = runner._resolve_turn_agent_config("hi", model, runtime)
    if accepted:
        assert tier == "ultrafast" and "only available" not in response
        assert route["request_overrides"] == {"service_tier": "ultrafast"}
    else:
        assert "only available" in response
        assert session_key not in runner._session_service_tier_overrides


@pytest.mark.asyncio
async def test_fast_override_lands_under_the_recovered_telegram_topic_key(monkeypatch, tmp_path):
    """/fast keys its eligibility check AND its tier override by the topic-recovered source the next
    turn uses (#30479), not the raw lobby-shaped event source."""
    import dataclasses

    runner = _make_runner()
    source = _make_source()
    monkeypatch.setattr(runner, "_recover_telegram_topic_thread_id", lambda src: "77")
    raw_key = runner._session_key_for_source(source)
    turn_key = runner._session_key_for_source(dataclasses.replace(source, thread_id="77"))
    assert turn_key != raw_key
    runner._session_model_overrides[turn_key] = dict(_ASTRA_ON_CODEX)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "claude-sonnet-4-6")

    response = await runner._handle_fast_command(MessageEvent(text="/fast fast", source=source, message_id="m1"))

    assert "FAST" in response
    assert runner._resolve_session_service_tier(session_key=turn_key) == "priority"
    assert raw_key not in runner._session_service_tier_overrides
