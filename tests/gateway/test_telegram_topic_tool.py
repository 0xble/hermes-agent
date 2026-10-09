"""``telegram_topic`` creates and edits Telegram DM topics as Hermes sessions.

Driven through the real gateway objects: a real ``SessionStore`` (routing write-through, restart
rehydration), a real ``SessionDB`` (titles, topic bindings, icon state) and the runner's own
``/model`` and ``/reasoning`` commit paths. Only Telegram and model resolution are faked.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource, SessionStore
from gateway.telegram_topic_sessions import TopicRequestError, TopicSpec, create_topic_session, edit_topic_session
from hermes_state import AsyncSessionDB, SessionDB

CHAT = "2027045491"
ICONS = [{"emoji": "🧵", "custom_emoji_id": "icon-thread"}, {"emoji": "💡", "custom_emoji_id": "icon-idea"}]


class FakeTelegram:
    """The Bot API surface the topic path uses, recording every call."""

    def __init__(self, thread_id=4242):
        self.thread_id = thread_id
        self.created, self.renamed, self.sent, self.admitted = [], [], [], []

    async def get_forum_topic_icon_options(self):
        return list(ICONS)

    async def _create_dm_topic(self, chat_id, name, icon_color=None, icon_custom_emoji_id=None):
        self.created.append({"chat_id": chat_id, "name": name, "icon": icon_custom_emoji_id})
        return self.thread_id

    async def rename_dm_topic(self, **kwargs):
        self.renamed.append(kwargs)
        return True

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return SimpleNamespace(success=True, message_id="m1")

    async def handle_message(self, event):
        self.admitted.append(event)
        event._gateway_accepted = True


def _source(thread_id=None):
    return SessionSource(platform=Platform.TELEGRAM, chat_id=CHAT, chat_type="dm", user_id=CHAT,
                         user_name="Brian", chat_name="Brian", thread_id=thread_id)


def _model_result(model="gpt-test", provider="openai-codex"):
    return SimpleNamespace(success=True, new_model=model, target_provider=provider, api_key="sk-x",
                           base_url="https://example.invalid/v1", api_mode="chat_completions",
                           request_overrides={}, runtime_capabilities={}, provider_label=provider,
                           warning_message="", model_info=None)


@pytest.fixture
def home(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.enable_telegram_topic_mode(chat_id=CHAT, user_id=CHAT)
    yield SimpleNamespace(path=tmp_path, db=db)
    db.close()


def _runner(home, adapter, *, auto_icons=False):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(
        enabled=True, token="***", extra={"auto_topic_icons": auto_icons})})
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.session_store = SessionStore(sessions_dir=home.path / "sessions", config=runner.config)
    # One state.db behind both the routing index and the title/binding store, as in production.
    runner.session_store._db = home.db
    runner._session_db = AsyncSessionDB(home.db)
    runner._agent_cache, runner._agent_cache_lock = {}, None
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._load_reasoning_config = lambda model="": {"enabled": True, "effort": "medium"}
    runner._delivery_adapter_for = lambda source: adapter
    runner._record_switch_metrics = MagicMock()
    runner._perform_model_switch = AsyncMock(return_value=(_model_result(), None))
    runner._model_selection_warning = AsyncMock(return_value=None)  # no pricing lookups in tests
    return runner


@pytest.mark.asyncio
async def test_create_binds_a_new_session_with_every_setting_and_starts_it(home):
    adapter = FakeTelegram()
    runner = _runner(home, adapter)

    result = await create_topic_session(runner, _source(thread_id="297206"), TopicSpec(
        name="Planning Room", icon="💡", model="gpt-test", provider="openai-codex",
        reasoning="xhigh", message="/title is not a command here: read the plan"))

    # Topic created in the caller's chat with the chosen icon, never inside the caller's topic.
    assert adapter.created == [{"chat_id": int(CHAT), "name": "Planning Room", "icon": "icon-idea"}]
    dest = _source(thread_id="4242")
    key = runner._session_key_for_source(dest)
    sid = result["session_id"]
    assert result["thread_id"] == "4242" and result["relay"] == f"hermes:default/{sid}"
    # Bound session, user-authority title, icon recorded as manual so auto-renames keep it.
    binding = home.db.get_telegram_topic_binding(chat_id=CHAT, thread_id="4242")
    assert binding["session_id"] == sid and binding["session_key"] == key
    assert home.db.get_session_title(sid) == "Planning Room"
    assert home.db.get_session_title_source(sid) == "user"
    assert home.db.get_telegram_topic_icon_state(CHAT, "4242")["owner"] == "manual"
    # The brief is shown in the topic and admitted as the session's first turn, never as a command.
    assert adapter.sent[0]["metadata"] == {"thread_id": "4242"}
    turn = adapter.admitted[0]
    assert turn.internal and not turn.allow_gateway_control and not turn.is_command()
    assert turn.source.thread_id == "4242" and turn.text.startswith("/title is not a command")
    # The turn is anchored on the shown brief, so the session's replies thread under it.
    assert turn.message_id == "m1" and turn.source.message_id == "m1"

    # A gateway restart keeps the chosen model and reasoning for that session.
    restarted = _runner(home, adapter)
    restarted._rehydrate_session_model_override(key)
    assert restarted._session_model_override(key)["model"] == "gpt-test"
    assert restarted._session_model_override(key)["provider"] == "openai-codex"
    assert restarted._resolve_session_reasoning_config(session_key=key) == {"enabled": True, "effort": "xhigh"}


@pytest.mark.asyncio
async def test_create_validates_everything_before_touching_telegram(home):
    adapter = FakeTelegram()
    runner = _runner(home, adapter)

    with pytest.raises(TopicRequestError, match="Unsupported topic icon"):
        await create_topic_session(runner, _source(), TopicSpec(name="Room", icon="🚀"))
    with pytest.raises(TopicRequestError, match="reasoning"):
        await create_topic_session(runner, _source(), TopicSpec(name="Room", reasoning="extreme"))
    runner._perform_model_switch = AsyncMock(return_value=(None, "Unknown model nope"))
    with pytest.raises(TopicRequestError, match="Unknown model"):
        await create_topic_session(runner, _source(), TopicSpec(name="Room", model="nope"))
    with pytest.raises(ValueError):
        await create_topic_session(runner, _source(), TopicSpec(name="x" * 200))
    assert adapter.created == []  # an invalid request never leaves an orphan topic


@pytest.mark.asyncio
async def test_create_without_icon_uses_the_automatic_pick(home):
    adapter = FakeTelegram()
    runner = _runner(home, adapter, auto_icons=True)
    runner._select_telegram_topic_icon = AsyncMock(
        return_value=("icon-thread", ("icon-thread", "🧵", "default"), ("🧵", "icon-thread", "default"), "auto"))

    await create_topic_session(runner, _source(), TopicSpec(name="Thread Work"))

    assert adapter.created[0]["icon"] == "icon-thread"
    assert home.db.get_telegram_topic_icon_state(CHAT, "4242")["owner"] == "auto"


@pytest.mark.asyncio
async def test_create_reports_the_created_topic_when_setup_fails(home):
    adapter = FakeTelegram()
    runner = _runner(home, adapter)
    runner.session_store.get_or_create_session = MagicMock(side_effect=RuntimeError("db locked"))

    with pytest.raises(TopicRequestError, match="Topic 4242 was created.*thread_id=4242"):
        await create_topic_session(runner, _source(), TopicSpec(name="Room"))


@pytest.mark.asyncio
async def test_create_refuses_a_model_that_needs_the_users_confirmation(home, monkeypatch):
    """Cost, data-training and large-context picks need the user's confirmation under /model; an
    agent cannot give it, so the tool refuses before any topic exists."""
    import hermes_cli.model_selection_guards as guards
    from hermes_cli.model_selection_guards import SelectionWarning

    adapter = FakeTelegram()
    runner = _runner(home, adapter)
    del runner._model_selection_warning  # the real seam /model also uses
    runner._cached_agent_for = lambda key: None
    seen = []

    def _warn(model, **kwargs):
        seen.append((model, kwargs.get("provider")))
        return SelectionWarning(kind="cost", title="Expensive Model Warning", model=model,
                                provider=kwargs.get("provider") or "", message="$$$ per million tokens")
    monkeypatch.setattr(guards, "combined_selection_warning", _warn)

    with pytest.raises(TopicRequestError, match=r"Expensive Model Warning: gpt-test needs the user's confirmation"):
        await create_topic_session(runner, _source(), TopicSpec(name="Room", model="gpt-test"))
    assert seen == [("gpt-test", "openai-codex")]
    assert adapter.created == []
    runner._perform_model_switch.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_reports_an_undelivered_brief_and_never_admits_it(home):
    adapter = FakeTelegram()
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=False, error="Forbidden: topic closed"))
    runner = _runner(home, adapter)

    with pytest.raises(TopicRequestError, match=r"opening brief was not delivered: Forbidden: topic closed\. "
                                                r"Send it with relay to hermes:default/"):
        await create_topic_session(runner, _source(), TopicSpec(name="Room", message="start here"))
    # The topic and its session exist and are bound; only the brief is missing, and the session never
    # starts working from a brief the user was not shown.
    assert home.db.get_telegram_topic_binding(chat_id=CHAT, thread_id="4242")["session_id"]
    assert adapter.admitted == []

    adapter.send = AsyncMock(side_effect=RuntimeError("network down"))
    with pytest.raises(TopicRequestError, match="not delivered: network down"):
        await create_topic_session(runner, _source(), TopicSpec(name="Room 2", message="start here"))
    assert adapter.admitted == []


@pytest.mark.asyncio
async def test_create_keeps_the_receiving_bots_routing_provenance(monkeypatch):
    """A multiplexed shared-bot turn must reach the bot that received it. The source goes through
    the gateway's real per-turn source cache, then the tool's copy, then the topic's copy, and
    every hop must keep the wire-invisible transport provenance."""
    import weakref
    from gateway.run import GatewayRunner
    from gateway.session_identity import RoutingIdentity, identity_of
    from tools import telegram_topic_tool as tool

    key = "agent:work:telegram:dm:2027045491:297206"
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_KEY", key)
    receiving_bot = FakeTelegram()
    calling = _source(thread_id="297206")
    calling.profile = "work"
    calling._transport_adapter_ref = weakref.ref(receiving_bot)
    pinned = RoutingIdentity(transport_profile="default", runtime_profile="work",
                             authorization_home=Path("/h"), runtime_home=Path("/h/profiles/work"))
    calling._identity = pinned
    seen = []

    runner = object.__new__(GatewayRunner)
    runner._cache_session_source(key, calling)  # what a turn does at admission (run_turn.py)

    def _delivery_adapter_for(source):
        seen.append((getattr(source, "_transport_adapter_ref", lambda: None)(), identity_of(source)))
        return receiving_bot

    runner._delivery_adapter_for = _delivery_adapter_for
    copied = tool._calling_source(runner)
    assert copied is not calling and copied._transport_adapter_ref() is receiving_bot and identity_of(copied) is pinned

    from gateway.telegram_topic_sessions import _adapter
    from gateway.session_identity import replace_source
    _adapter(runner, replace_source(copied, thread_id=None), "_create_dm_topic")
    assert seen == [(receiving_bot, pinned)]


@pytest.mark.asyncio
async def test_a_create_that_failed_after_telegram_is_finished_by_the_suggested_edit(home):
    adapter = FakeTelegram()
    runner = _runner(home, adapter)
    bind = runner._record_telegram_topic_binding
    runner._record_telegram_topic_binding = MagicMock(side_effect=RuntimeError("db locked"))

    with pytest.raises(TopicRequestError, match="action=edit thread_id=4242 name='Room' reasoning='high'") as err:
        await create_topic_session(runner, _source(), TopicSpec(name="Room", reasoning="high"))
    assert home.db.get_telegram_topic_binding(chat_id=CHAT, thread_id="4242") is None
    assert "action=edit" in str(err.value)

    runner._record_telegram_topic_binding = bind
    result = await edit_topic_session(runner, _source(), "4242", TopicSpec(name="Room", reasoning="high"))

    binding = home.db.get_telegram_topic_binding(chat_id=CHAT, thread_id="4242")
    assert binding["session_id"] == result["session_id"]
    assert result["relay"] == f"hermes:default/{result['session_id']}"
    assert home.db.get_session_title(result["session_id"]) == "Room"
    key = runner._session_key_for_source(_source(thread_id="4242"))
    assert runner._resolve_session_reasoning_config(session_key=key) == {"enabled": True, "effort": "high"}


@pytest.mark.asyncio
async def test_a_model_edit_after_restart_resolves_against_the_topics_own_route(home, tmp_path, monkeypatch):
    """After a restart the in-memory override is empty until the next turn; a model-only edit must
    still resolve against the topic's persisted provider, not the global config's."""
    import gateway.run as gateway_run

    (tmp_path / "config.yaml").write_text("model:\n  default: global-model\n  provider: openrouter\n")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    adapter = FakeTelegram()
    runner = _runner(home, adapter)
    await create_topic_session(runner, _source(), TopicSpec(name="Room", model="gpt-test", provider="openai-codex"))

    restarted = _runner(home, adapter)
    restarted._is_session_running = lambda key: False
    await edit_topic_session(restarted, _source(), "4242", TopicSpec(model="gpt-test-2"))

    ctx = restarted._perform_model_switch.await_args.args[0]
    assert (ctx.current_model, ctx.current_provider) == ("gpt-test", "openai-codex")


@pytest.mark.asyncio
async def test_edit_follows_a_restored_binding_instead_of_repointing_the_topic(home):
    """`/topic <id>` restores an older session into a topic; the route moves only on the next
    message. An edit in between must act on the restored session and keep the binding, as a turn
    would, not rebind the topic to whatever the stale route points at."""
    adapter = FakeTelegram()
    runner = _runner(home, adapter)
    created = await create_topic_session(runner, _source(), TopicSpec(name="Room"))
    older = home.db.create_session(session_id="older-session", source="telegram")
    home.db.bind_telegram_topic(chat_id=CHAT, thread_id="4242", user_id=CHAT,
                                session_key=runner._session_key_for_source(_source(thread_id="4242")),
                                session_id="older-session")

    result = await edit_topic_session(runner, _source(), "4242", TopicSpec(name="Renamed", reasoning="low"))

    assert result["session_id"] == "older-session" != created["session_id"]
    assert home.db.get_telegram_topic_binding(chat_id=CHAT, thread_id="4242")["session_id"] == "older-session"
    assert home.db.get_session_title("older-session") == "Renamed"
    assert home.db.get_session_title(created["session_id"]) == "Room"
    del older


@pytest.mark.asyncio
async def test_a_model_edit_on_a_restored_topic_resolves_and_lands_on_the_topics_route(home):
    """Model and reasoning overrides and the running-turn slot belong to the topic's route
    (session_key), and the binding heal's switch_session carries both overrides onto the restored
    session, so resolving before the heal reads the same route state the heal leaves behind.
    Resolving first keeps an invalid pick from binding or switching anything."""
    adapter = FakeTelegram()
    runner = _runner(home, adapter)
    created = await create_topic_session(runner, _source(), TopicSpec(
        name="Room", model="gpt-test", provider="openai-codex", reasoning="high"))
    key = runner._session_key_for_source(_source(thread_id="4242"))
    home.db.create_session(session_id="older-session", source="telegram")
    home.db.bind_telegram_topic(chat_id=CHAT, thread_id="4242", user_id=CHAT, session_key=key,
                                session_id="older-session")
    seen_running = []
    runner._is_session_running = lambda k: seen_running.append(k) or False

    # An invalid pick is refused before the heal, so it neither switches the route nor touches overrides.
    runner._perform_model_switch.return_value = (None, "unknown model")
    with pytest.raises(TopicRequestError, match="unknown model"):
        await edit_topic_session(runner, _source(), "4242", TopicSpec(model="nope"))
    assert runner.session_store.peek_session_id(key) == created["session_id"]
    assert runner.session_store.get_model_override(key)["model"] == "gpt-test"

    runner._perform_model_switch.return_value = (_model_result("gpt-test-2"), None)
    result = await edit_topic_session(runner, _source(), "4242", TopicSpec(model="gpt-test-2"))

    ctx = runner._perform_model_switch.await_args.args[0]
    assert (ctx.session_key, ctx.current_model, ctx.current_provider) == (key, "gpt-test", "openai-codex")
    assert set(seen_running) == {key}
    assert result["session_id"] == "older-session" != created["session_id"]
    assert runner.session_store.peek_session_id(key) == "older-session"
    assert runner.session_store.get_model_override(key)["model"] == "gpt-test-2"
    assert runner.session_store.get_reasoning_override(key)["effort"] == "high"


@pytest.mark.asyncio
async def test_a_settings_only_edit_of_an_unknown_topic_is_refused(home):
    adapter = FakeTelegram()
    runner = _runner(home, adapter)

    with pytest.raises(TopicRequestError, match="No topic 9999 in this chat is known"):
        await edit_topic_session(runner, _source(), "9999", TopicSpec(reasoning="low"))
    assert home.db.get_telegram_topic_binding(chat_id=CHAT, thread_id="9999") is None
    assert runner.session_store.peek_session_id(runner._session_key_for_source(_source(thread_id="9999"))) is None


@pytest.mark.asyncio
async def test_a_model_commit_rechecks_that_the_session_is_still_idle(home):
    """Resolving can block for seconds; a turn that starts meanwhile must not get its client
    swapped. The commit re-checks under the /model lock."""
    adapter = FakeTelegram()
    runner = _runner(home, adapter)
    await create_topic_session(runner, _source(), TopicSpec(name="Room"))
    checks = iter([False, True])  # idle when the edit starts, running by commit time
    runner._is_session_running = lambda key: next(checks)

    with pytest.raises(TopicRequestError, match="started a turn"):
        await edit_topic_session(runner, _source(), "4242", TopicSpec(model="gpt-test"))
    key = runner._session_key_for_source(_source(thread_id="4242"))
    assert runner._session_model_override(key) is None


@pytest.mark.asyncio
async def test_a_paused_hermes_starts_no_brief(home, monkeypatch):
    import agent.estop as estop

    monkeypatch.setattr(estop, "paused_reply", lambda: "Hermes is paused.")
    adapter = FakeTelegram()
    runner = _runner(home, adapter)

    with pytest.raises(TopicRequestError, match="opening brief was not delivered: Hermes is paused"):
        await create_topic_session(runner, _source(), TopicSpec(name="Room", message="start"))
    assert adapter.sent == [] and adapter.admitted == []


@pytest.mark.asyncio
async def test_a_new_topic_gets_no_model_switch_note_and_an_edit_does(home):
    adapter = FakeTelegram()
    runner = _runner(home, adapter)
    await create_topic_session(runner, _source(), TopicSpec(name="Room", model="gpt-test"))
    key = runner._session_key_for_source(_source(thread_id="4242"))
    assert key not in runner._pending_model_notes

    runner._is_session_running = lambda key: False
    await edit_topic_session(runner, _source(), "4242", TopicSpec(model="gpt-test"))
    assert "model was just switched" in runner._pending_model_notes[key]


def test_relay_address_names_the_gateways_own_profile_when_the_source_has_none():
    from gateway.telegram_topic_sessions import relay_address

    named = SimpleNamespace(_primary_profile_name="work", _active_profile_name=lambda: "default")
    assert relay_address(named, _source(), "sid") == "hermes:work/sid"
    routed = _source()
    routed.profile = "ops"
    assert relay_address(named, routed, "sid") == "hermes:ops/sid"
    standalone = SimpleNamespace(_active_profile_name=lambda: "lab")
    assert relay_address(standalone, _source(), "sid") == "hermes:lab/sid"


@pytest.mark.asyncio
async def test_edit_renames_topic_and_session_and_changes_settings(home):
    adapter = FakeTelegram()
    runner = _runner(home, adapter)
    created = await create_topic_session(runner, _source(), TopicSpec(name="Old Name"))

    result = await edit_topic_session(runner, _source(thread_id="297206"), "4242", TopicSpec(
        name="New Name", icon="🧵", model="gpt-test", reasoning="low"))

    assert adapter.renamed[-1] == {"chat_id": CHAT, "thread_id": "4242", "name": "New Name",
                                   "icon_custom_emoji_id": "icon-thread"}
    assert result["title"] == "New Name" and home.db.get_session_title(created["session_id"]) == "New Name"
    key = runner._session_key_for_source(_source(thread_id="4242"))
    assert runner._session_model_override(key)["model"] == "gpt-test"
    assert runner._resolve_session_reasoning_config(session_key=key) == {"enabled": True, "effort": "low"}


@pytest.mark.asyncio
async def test_edit_icon_only_keeps_the_visible_name(home):
    adapter = FakeTelegram()
    runner = _runner(home, adapter)
    await create_topic_session(runner, _source(), TopicSpec(name="Keep Me"))

    await edit_topic_session(runner, _source(), "4242", TopicSpec(icon="🧵"))

    assert adapter.renamed[-1]["name"] is None


@pytest.mark.asyncio
async def test_edit_refuses_a_model_change_mid_turn_and_the_general_topic(home):
    adapter = FakeTelegram()
    runner = _runner(home, adapter)
    runner._is_session_running = lambda key: True

    with pytest.raises(TopicRequestError, match="mid-turn"):
        await edit_topic_session(runner, _source(), "4242", TopicSpec(model="gpt-test"))
    with pytest.raises(TopicRequestError, match="General"):
        await edit_topic_session(runner, _source(), "1", TopicSpec(name="x"))
    assert adapter.renamed == []


def test_tool_is_available_when_only_a_multiplexed_profile_has_telegram(monkeypatch):
    """A secondary profile's Telegram bot lives in ``_profile_adapters``, not the primary's map."""
    from tools import telegram_topic_tool as tool

    runner = SimpleNamespace(adapters={Platform.DISCORD: object()}, _profile_adapters={"work": {}})
    monkeypatch.setattr(tool, "_live_runner", lambda: runner)
    assert tool.check_telegram_topic_tool() is False
    runner._profile_adapters["work"] = {Platform.TELEGRAM: object()}
    assert tool.check_telegram_topic_tool() is True
    monkeypatch.setattr(tool, "_live_runner", lambda: None)
    assert tool.check_telegram_topic_tool() is False


def test_tool_reaches_only_telegram_sessions():
    from toolsets import resolve_toolset
    from tools.telegram_topic_tool import TELEGRAM_TOPIC_SCHEMA

    assert "telegram_topic" in resolve_toolset("hermes-telegram")
    assert "telegram_topic" not in resolve_toolset("hermes-cli")
    assert "telegram_topic" not in resolve_toolset("hermes-discord")
    assert TELEGRAM_TOPIC_SCHEMA["parameters"]["additionalProperties"] is False


def test_saved_telegram_tool_list_gains_the_tool_without_a_config_edit():
    """An explicitly saved Telegram toolset list must still reach the tool."""
    from hermes_cli.tools_config import _get_platform_tools

    saved = {"platform_toolsets": {"telegram": ["terminal", "file", "web", "memory", "skills"]}}
    assert "telegram_topic" in _get_platform_tools(saved, "telegram")
    assert "telegram_topic" not in _get_platform_tools({"platform_toolsets": {"cli": ["terminal"]}}, "cli")


@pytest.mark.asyncio
async def test_tool_returns_json_and_rejects_non_dm_callers(monkeypatch):
    from tools import telegram_topic_tool as tool

    monkeypatch.setattr(tool, "_live_runner", lambda: SimpleNamespace(_get_cached_session_source=lambda key: None))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    error = json.loads(await tool.telegram_topic_tool({"action": "create", "name": "x"}))["error"]
    assert "only from a Telegram DM" in error
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", CHAT)
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "dm")
    assert "name is required" in json.loads(await tool.telegram_topic_tool({"action": "create"}))["error"]
    edit = json.loads(await tool.telegram_topic_tool({"action": "edit", "message": "hi"}))
    assert "only for action=create" in edit["error"]
