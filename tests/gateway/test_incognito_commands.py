"""Exercise slash dispatch, provider resolution, durable queues and restart boundaries."""
import asyncio
import json
import secrets
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from gateway.run import GatewayRunner
from gateway.config import GatewayConfig, Platform
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from gateway.platforms.event import MessageEvent, MessageType
from hermes_cli.model_switch import ModelSwitchResult
from hermes_state import SessionDB


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway import run
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    config = {"model": {"default": "normal-model", "provider": "openrouter"}}
    monkeypatch.setattr(run, "_load_gateway_config", lambda: config)
    db = SessionDB(db_path=tmp_path / "state.db")
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    store._db = db
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._running_agents = {}
    runner._pending_one_turn_model_restores = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.RLock()
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="4", thread_id="9")
    entry = store.get_or_create_session(source)
    def event(text):
        return MessageEvent(text=text, source=source, message_type=MessageType.TEXT)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", dict)
    yield runner, store, entry, config, event, db
    from agent.incognito import register_session
    register_session(entry.session_id, False)
    register_session(entry.session_key, False)
    db.close()


@pytest.mark.asyncio
async def test_toggle_status_busy_and_normal_identity(harness):
    runner, store, entry, _config, event, _db = harness
    handler = runner._gateway_plain_command_handlers()["incognito"]
    assert "OFF" in await handler(event("/incognito status"))
    assert "ON" in await handler(event("/incognito"))
    assert "normal-model" in await handler(event("/incognito status"))
    runner._running_agents[entry.session_key] = object()
    assert "not changed" in await handler(event("/incognito off"))
    assert "ON" in await handler(event("/incognito status"))
    runner._running_agents.clear()
    assert "OFF" in await handler(event("/incognito off"))
    assert store.get_or_create_session(event("").source).session_id == entry.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", [None, {"model": "prior-model", "provider": "openrouter"}])
async def test_default_model_uses_model_resolution_then_restores(harness, monkeypatch, prior):
    runner, store, entry, config, event, db = harness
    if prior:
        store.set_model_override(entry.session_key, prior)
        runner._session_model_overrides[entry.session_key] = prior
    config["incognito"] = {"model": "openrouter/private-model"}
    seen = []
    def resolve(**kw):
        seen.append(kw)
        return ModelSwitchResult(success=True, new_model="private-model", target_provider="openrouter")
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", resolve)
    answer = await runner._handle_incognito_command(event("/incognito on"))
    assert "ON" in answer and "private-model" in answer
    assert seen
    assert entry.model_override["model"] == "private-model"
    restored_store = SessionStore(store.sessions_dir, GatewayConfig())
    restored_store._db = db
    restored = restored_store.lookup_by_session_key(entry.session_key)
    assert restored.incognito and restored.model_override["model"] == "private-model"
    assert restored_store.incognito_metadata(entry.session_key)["model_override"] == prior
    answer = await runner._handle_incognito_command(event("/incognito off"))
    assert "OFF" in answer
    assert entry.model_override == prior
    assert runner._session_model_override(entry.session_key) == prior


@pytest.mark.asyncio
async def test_invalid_model_keeps_privacy_on(harness, monkeypatch):
    runner, _store, entry, config, event, _db = harness
    config["incognito"] = {"model": "not-a-route"}
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **kw: ModelSwitchResult(success=False, error_message="Unavailable model"))
    result = await runner._handle_incognito_command(event("/incognito on"))
    assert "remains ON" in result and "Unavailable model" in result
    assert entry.incognito and entry.model_override is None


@pytest.mark.asyncio
async def test_actual_ingress_scope_precedes_admission_and_response_storage(harness, monkeypatch, tmp_path, caplog):
    from agent.incognito import is_incognito, incognito_scope
    from gateway import outbox, delivery_ledger, stream_recovery
    from gateway.shutdown_flush import flush_pending_to_file
    runner, _store, entry, _config, event, _db = harness
    await runner._handle_incognito_command(event("/incognito on"))
    secret = secrets.token_hex(24)
    seen = []
    async def admit(ev):
        seen.append(is_incognito())
        assert is_incognito(entry.session_id)
        # The real gateway ingress calls this before admission, hooks or message logging.
        caplog.handler.emit(__import__("logging").LogRecord("test", 20, "", 0, "private probe", (), None))
        private = outbox.store_for(tmp_path)
        turn, _fresh = private.admit("default", "telegram", "private-id", "message")
        private.finish_admission(turn, secret)
        private.enqueue(turn, "text", {"text": secret})
        assert private.original_result(turn) == secret
        stream_recovery.record_preview(turn_id=turn, session_key=entry.session_key,
                                       session_id=entry.session_id, content=secret, message_id="m")
        from gateway.rich_sent_store import record_async
        await record_async("123", "m", secret)
        assert flush_pending_to_file({entry.session_key: ev}) == 0
    monkeypatch.setattr(runner, "_hm_admit_event", admit)
    await runner._handle_message(event(secret))
    assert seen == [True]
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path
    # A public turn still uses the normal on-disk outbox.
    public = outbox.store_for(tmp_path)
    assert not public._private
    turn, _ = public.admit("default", "telegram", "normal-id", "message")
    public.finish_admission(turn, "saved public reply")
    assert public.original_result(turn) == "saved public reply"


def test_config_key_is_exposed():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["incognito"]["model"] == ""


@pytest.mark.asyncio
async def test_two_private_tool_turns_off_then_public_persistence(harness, monkeypatch, tmp_path):
    from agent.incognito import incognito_scope
    runner, store, entry, _config, event, db = harness
    runner._session_db = db
    # Autospec keeps the real signatures: a bare AsyncMock hid a wrong-arity private call.
    for name in ("_hmwa_store_turn_prompt_tokens", "_refresh_agent_cache_message_count"):
        monkeypatch.setattr(runner, name, create_autospec(getattr(runner, name)))
    sid = entry.session_id
    for row in ({"role": "user", "content": "original question"}, {"role": "assistant", "content": "original answer"}):
        store.append_to_transcript(sid, row)
    assert db.search_messages("original")
    await runner._handle_incognito_command(event("/incognito on"))
    secret = secrets.token_hex(24)
    async def persist(content, private):
        history = store.load_transcript(sid)
        rows = [{"role": "user", "content": content},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "t", "type": "function", "function": {"name": "recall", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "t", "content": content},
                {"role": "assistant", "content": content}]
        prepared = SimpleNamespace(history=history, persist_user_message=content, message_text=content,
                                   persist_user_timestamp=None, persist_user_display_kind=None, persistence_owner=None)
        with incognito_scope(private, sid):
            await runner._hmwa_persist_turn_transcript(event=event(content), source=event("").source,
                session_entry=entry, session_key=entry.session_key,
                agent_result={"agent_persisted": private, "history_offset": len(history)},
                agent_messages=history + rows, prepared=prepared, response=content,
                agent_failed_early=False, hidden_reasoning_incomplete=False, is_context_overflow_failure=False)
    await persist(secret + "first", True)
    await persist(secret + "second", True)
    assert secret + "first" in json.dumps(store.load_transcript(sid))
    assert secret + "second" in json.dumps(store.load_transcript(sid))
    assert db.search_messages("original")  # recall remains readable in the private interval
    assert not db.search_messages(secret)
    await runner._handle_incognito_command(event("/incognito off"))
    await persist("public after off", False)
    assert entry.session_id == sid
    saved = json.dumps(store.load_transcript(sid))
    assert "original question" in saved and "public after off" in saved and secret not in saved
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path


@pytest.mark.asyncio
async def test_unset_model_keeps_original_cached_agent_and_frozen_prompt(harness):
    runner, _store, entry, _config, event, _db = harness
    calls = []
    frozen = object()
    agent = SimpleNamespace(model="normal-model", _cached_system_prompt=frozen, set_incognito=calls.append)
    slot = (agent, "normal-signature")
    runner._agent_cache[entry.session_key] = slot
    runner._pending_model_notes = {entry.session_key: "original note"}
    state = runner._session_state(entry.session_key)
    state.conversation.ephemeral_pin = ("original", "frozen context", False)
    await runner._handle_incognito_command(event("/incognito on"))
    state.conversation.ephemeral_pin = ("private", "private context", False)
    runner._pending_model_notes[entry.session_key] = "private note"
    await runner._handle_incognito_command(event("/incognito off"))
    assert runner._pending_model_notes[entry.session_key] == "original note"
    assert runner._session_state(entry.session_key).conversation.ephemeral_pin == ("original", "frozen context", False)
    assert calls == [True, False]
    assert runner._agent_cache[entry.session_key] is slot
    assert agent._cached_system_prompt is frozen


def test_config_command_round_trip(tmp_path, monkeypatch, capsys):
    from hermes_cli import config
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(config, "get_hermes_home", lambda: tmp_path)
    config.config_command(SimpleNamespace(config_command="set", key="incognito.model", value="openrouter/private-model"))
    config.config_command(SimpleNamespace(config_command="get", key="incognito.model"))
    assert "openrouter/private-model" in capsys.readouterr().out
    config.config_command(SimpleNamespace(config_command="unset", key="incognito.model"))
    import hermes_yaml
    assert "model" not in (hermes_yaml.safe_load((tmp_path / "config.yaml").read_text()).get("incognito") or {})


@pytest.mark.asyncio
async def test_adapter_ingress_and_mock_wire_use_only_ram_outbox(harness, tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO)
    from agent.incognito import is_incognito
    from gateway.outbox import Outbox
    from tests.gateway.test_durable_outbox import _final_fixture
    runner, _store, _entry, _config, event, _db = harness
    await runner._handle_incognito_command(event("/incognito on"))
    secret = secrets.token_hex(24)
    sends, tasks = [], []
    adapter = _final_fixture(tmp_path, sends)
    wire_runner = adapter.gateway_runner
    async def handle(ev):
        assert is_incognito()
        admitted = await wire_runner._hm_admit_event(ev)
        assert admitted
        return secret + " reply"
    wire_runner._handle_admitted_message = handle
    adapter._finish_session_task = lambda key, guard: adapter._active_sessions.pop(key, None)
    def start(ev, key):
        tasks.append(asyncio.create_task(adapter._process_message_background(ev, key)))
        return True
    adapter._start_session_processing = start
    private = event(secret)
    private.message_id = "private-message"
    await adapter.handle_message(private)
    assert tasks
    await tasks[-1]
    assert sends == [secret + " reply"]
    assert secret not in caplog.text
    assert Outbox(tmp_path).all_rows() == []
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path


@pytest.mark.asyncio
async def test_new_ingress_is_refused_until_transition_completes(harness, monkeypatch, tmp_path):
    from agent.incognito import is_incognito
    from gateway.incognito import transition_pending
    runner, _store, entry, config, event, _db = harness
    config["incognito"] = {"model": "openrouter/private-model"}
    entered, release = asyncio.Event(), asyncio.Event()
    async def resolve(*a):
        entered.set()
        await release.wait()
        return ModelSwitchResult(success=True, new_model="private-model", target_provider="openrouter"), None
    monkeypatch.setattr(runner, "_perform_model_switch", resolve)
    toggle = asyncio.create_task(runner._handle_incognito_command(event("/incognito on")))
    await entered.wait()
    assert transition_pending(entry.session_key)
    admission = AsyncMock(side_effect=AssertionError("must not admit during a switch"))
    monkeypatch.setattr(runner, "_hm_admit_event", admission)
    assert "Retry" in await runner._handle_message(event("not admitted"))
    admission.assert_not_awaited()
    release.set()
    assert "ON" in await toggle
    assert not transition_pending(entry.session_key)


@pytest.mark.asyncio
async def test_status_command_includes_incognito_and_actual_model(harness, monkeypatch):
    from tests.gateway.test_status_command import _make_runner
    runner, _store, entry, _config, event, _db = harness
    await runner._handle_incognito_command(event("/incognito on"))
    status = _make_runner(entry)
    status._running_agents[entry.session_key] = SimpleNamespace(model="active-private-model", provider="openrouter")
    monkeypatch.setattr(status, "_resolve_route_context", AsyncMock(return_value=None))
    reply = await status._handle_status_command(event("/status"))
    assert "Incognito: ON" in reply and "active-private-model" in reply


@pytest.mark.asyncio
async def test_off_drops_private_results_without_touching_other_topics(harness, tmp_path):
    from gateway.incognito import transition_scope, discard_private_topic
    from gateway.outbox import store_for
    runner, _store, entry, _config, event, _db = harness
    await runner._handle_incognito_command(event("/incognito on"))
    key, other = entry.session_key, entry.session_key + ":other"
    with transition_scope(key):
        first = store_for(tmp_path)
        turn, _ = first.admit("default", "telegram", "same-id", "message")
        first.finish_admission(turn, "private first topic")
    with transition_scope(other):
        second = store_for(tmp_path)
        second_turn, _ = second.admit("default", "telegram", "same-id", "message")
        second.finish_admission(second_turn, "private second topic")
    await runner._handle_incognito_command(event("/incognito off"))
    with transition_scope(key):
        fresh = store_for(tmp_path)
        assert fresh is not first
        assert fresh.admit("default", "telegram", "same-id", "message")[1]
        assert fresh.original_result(turn) is None
    with transition_scope(other):
        assert store_for(tmp_path) is second
        assert second.original_result(second_turn) == "private second topic"
    discard_private_topic(key)
    discard_private_topic(other)


@pytest.mark.asyncio
async def test_private_compression_keeps_identity_and_only_rewrites_ram(harness, tmp_path):
    from agent.incognito import incognito_scope
    from gateway.run_turn_runner import TurnRunner
    runner, store, entry, _config, event, db = harness
    store.append_to_transcript(entry.session_id, {"role": "user", "content": "saved before compression"})
    await runner._handle_incognito_command(event("/incognito on"))
    secret = secrets.token_hex(24)
    with incognito_scope(True, entry.session_id):
        compressor = object.__new__(TurnRunner)
        agent = SimpleNamespace(session_id="volatile-child", _last_compaction_in_place=False)
        compressor._ctx = SimpleNamespace(agent_holder=[agent], session_id=entry.session_id)
        compressor._runner = runner
        compacted, effective, offset = compressor._sync_session_after_run([{"role": "user"}])
        assert compacted and effective == entry.session_id and offset == 0
        assert agent.session_id == entry.session_id
        assert store.rewrite_transcript(effective, [{"role": "user", "content": secret}])
        assert store.load_transcript(effective)[0]["content"] == secret
    await runner._handle_incognito_command(event("/incognito off"))
    assert store.load_transcript(entry.session_id)[0]["content"] == "saved before compression"
    assert db.get_session("volatile-child") is None
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path


@pytest.mark.asyncio
async def test_first_command_in_a_fresh_topic_persists_only_its_boundary(harness):
    from agent.incognito import incognito_scope, register_session
    runner, store, _original, _config, _event, db = harness
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="4", thread_id="fresh")
    ev = MessageEvent(text="/incognito on", message_type=MessageType.TEXT, source=source)
    with incognito_scope(True):
        assert "ON" in await runner._handle_incognito_command(ev)
    entry = store.lookup_by_session_key(runner._session_key_for_source(source))
    restart = SessionStore(store.sessions_dir, GatewayConfig())
    restart._db = db
    restored = restart.lookup_by_session_key(entry.session_key)
    assert restored.incognito and restored.session_id == entry.session_id
    assert db.get_session(entry.session_id) is not None
    store.set_incognito(entry.session_key, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/model private --global", "/fork private", "/goal private", "/refine private",
                                  "/topic 20261030_000000_abcdef"])
async def test_durable_slash_commands_refused_before_admission(harness, monkeypatch, text):
    runner, _store, _entry, _config, event, _db = harness
    await runner._handle_incognito_command(event("/incognito on"))
    admission = AsyncMock(side_effect=AssertionError("private durable slash reached admission"))
    monkeypatch.setattr(runner, "_hm_admit_event", admission)
    assert "unavailable in incognito" in await runner._handle_message(event(text))
    admission.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("multiplexed", [False, True])
async def test_adapter_refuses_an_ambiguous_private_lane_before_boundary_creation(harness, multiplexed):
    runner, store, entry, _config, event, _db = harness
    receiver = SimpleNamespace(
        incognito_target_refusal=lambda source: "Incognito is unavailable in this shared conversation."
    )
    incoming = event("/incognito on")
    if multiplexed:
        runner.config.multiplex_profiles = True
        runner._profile_adapters = {"other": {Platform.TELEGRAM: receiver}}
        incoming.source._transport_adapter_ref = lambda: receiver
        # The default bot must not override the receiving bot's privacy decision.
        runner.adapters[Platform.TELEGRAM] = SimpleNamespace(incognito_target_refusal=lambda source: None)
    else:
        runner.adapters[Platform.TELEGRAM] = receiver
    answer = await runner._handle_incognito_command(incoming)
    assert "unavailable" in answer
    assert not entry.incognito
    assert not (store.sessions_dir / "incognito.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["ignored", "/incognito on", "/goal ignored", "/model ignored --global"])
@pytest.mark.parametrize("ignored,chat_id", [
    (["C_IGNORED"], "C_IGNORED"),
    (["C_IGNORED"], "C_IGNORED:1712345678.000001"),
    (["*"], "C_OTHER"),
])
async def test_ignored_slack_ingress_precedes_privacy_and_command_replies(
    harness, monkeypatch, tmp_path, text, ignored, chat_id
):
    from unittest.mock import Mock
    from agent.incognito import incognito_scope
    from gateway.config import PlatformConfig
    runner, _store, _entry, _config, _event, _db = harness
    runner.config = GatewayConfig(platforms={
        Platform.SLACK: PlatformConfig(enabled=True, extra={"ignored_channels": ignored}),
    })
    unloaded = SessionStore(tmp_path / "unloaded" / "sessions", runner.config)
    privacy_lookup = Mock(wraps=unloaded.incognito_enabled)
    monkeypatch.setattr(unloaded, "incognito_enabled", privacy_lookup)
    runner.session_store = unloaded
    admission = AsyncMock(side_effect=AssertionError("ignored event reached admission"))
    monkeypatch.setattr(runner, "_hm_admit_event", admission)
    incoming = MessageEvent(text=text, message_type=MessageType.TEXT,
                            source=SessionSource(platform=Platform.SLACK, chat_id=chat_id, user_id="4"))
    with incognito_scope(True):
        assert await runner._handle_message(incoming) is None
    privacy_lookup.assert_not_called()
    admission.assert_not_awaited()
    assert not unloaded._loaded
    assert not unloaded.sessions_dir.exists()


def test_ingress_preflight_uses_canonical_intake_policy_not_runtime_bot(harness, monkeypatch):
    from gateway.config import PlatformConfig
    runner, _store, _entry, _config, _event, _db = harness
    runner.config = GatewayConfig(multiplex_profiles=True, platforms={
        Platform.SLACK: PlatformConfig(enabled=True, extra={"ignored_channels": ["C_DEFAULT"]}),
    })
    intake = SimpleNamespace(config=PlatformConfig(enabled=True, extra={"ignored_channels": ["C_INTAKE"]}))
    calls = []
    def canonicalize(source):
        calls.append("identity")
        source.profile = "runtime"
    def receiving_adapter(source):
        assert source.profile == "runtime"
        calls.append("intake")
        return intake
    monkeypatch.setattr(runner, "_canonicalize", canonicalize)
    monkeypatch.setattr(runner, "_intake_adapter_for", receiving_adapter)
    for chat_id, accepted in [("C_INTAKE", False), ("C_DEFAULT", True)]:
        incoming = MessageEvent(text="ignored", message_type=MessageType.TEXT,
                                source=SessionSource(platform=Platform.SLACK, chat_id=chat_id, user_id="4"))
        assert runner._hm_preflight_event(incoming) is accepted
    assert calls == ["identity", "intake", "identity", "intake"]


@pytest.mark.asyncio
async def test_internal_slack_event_bypasses_channel_gate_but_keeps_privacy(harness, monkeypatch):
    from agent.incognito import is_incognito
    from gateway.config import PlatformConfig
    runner, store, _original, _config, _event, _db = harness
    runner.config = GatewayConfig(platforms={
        Platform.SLACK: PlatformConfig(enabled=True, extra={"ignored_channels": ["C_PRIVATE"]}),
    })
    source = SessionSource(platform=Platform.SLACK, chat_id="C_PRIVATE", user_id="4")
    entry = store.get_or_create_session(source)
    store.set_incognito(entry.session_key, True)
    seen = []
    async def admit(incoming):
        seen.append(is_incognito())
    monkeypatch.setattr(runner, "_hm_admit_event", admit)
    incoming = MessageEvent(text="internal", message_type=MessageType.TEXT, source=source, internal=True)
    try:
        assert await runner._handle_message(incoming) is None
        assert seen == [True]
    finally:
        store.set_incognito(entry.session_key, False)


@pytest.mark.asyncio
async def test_new_in_private_topic_binds_titles_and_announces_the_public_session(harness, monkeypatch):
    """/new ends the private interval. Its binding, title and reset hook are public writes."""
    from agent.incognito import is_incognito
    from gateway.incognito import event_scope, private_topic
    runner, store, entry, _config, event, db = harness
    runner._session_db = db
    await runner._handle_incognito_command(event("/incognito on"))
    seen = {}

    def record(name):
        def observe(*args, **kwargs):
            seen[name] = (is_incognito(), private_topic())
        return observe

    async def titled(header, session_id, title):
        record("title")()
        return header

    monkeypatch.setattr(runner, "_hm_stop_owner_turn", AsyncMock())
    monkeypatch.setattr(runner, "_cleanup_old_agent_for_reset", AsyncMock())
    monkeypatch.setattr(runner, "_clear_conversation_scope_locked_async", AsyncMock())
    monkeypatch.setattr(runner, "_fire_session_reset_hooks", AsyncMock(side_effect=record("hooks")))
    monkeypatch.setattr(runner, "_reset_notice_session_info", lambda source: "")
    monkeypatch.setattr(runner, "_telegram_topic_new_header", lambda source: "")
    monkeypatch.setattr(runner, "_is_telegram_topic_lane", lambda source: True)
    monkeypatch.setattr(runner, "_record_telegram_topic_binding", record("binding"))
    monkeypatch.setattr(runner, "_reset_titled_header", titled)
    monkeypatch.setattr("gateway.session_automation.move_route_wakes", AsyncMock())
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", record("on_session_reset"))
    new = event("/new Public title")
    async with event_scope(runner, new):
        assert is_incognito()
        await runner._handle_reset_command(new)
        assert is_incognito()  # the caller's own scope is restored afterward
    assert seen == {name: (False, None) for name in ("hooks", "title", "binding", "on_session_reset")}
    fresh = store.lookup_by_session_key(entry.session_key)
    assert fresh.session_id != entry.session_id and not fresh.incognito


@pytest.mark.asyncio
async def test_off_rebuilds_an_agent_that_was_built_private(harness):
    """An agent born private never had the configured context engine; off must not keep it."""
    runner, _store, entry, _config, event, _db = harness
    await runner._handle_incognito_command(event("/incognito on"))
    born_private = SimpleNamespace(model="normal-model", _incognito_born=True, set_incognito=lambda enabled: None)
    runner._agent_cache[entry.session_key] = (born_private, "private-signature")
    assert "OFF" in await runner._handle_incognito_command(event("/incognito off"))
    assert runner._cached_agent_for(entry.session_key) is None
