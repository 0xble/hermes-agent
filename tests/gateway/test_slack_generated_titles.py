"""Slack consumes the shared titler's LLM stage once, without blocking the turn."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.config import Platform, PlatformConfig
from tests.gateway.test_slack import SlackAdapter


@pytest.fixture
def adapter():
    instance = SlackAdapter(PlatformConfig(enabled=True, token="***"))
    instance._app = MagicMock()
    instance._app.client = AsyncMock()
    return instance


@pytest.fixture
def source():
    return SessionSource(platform=Platform.SLACK, chat_id="D123", chat_type="dm",
                         thread_id="171234.0001", scope_id="T123", user_id="U123")


class Store:
    def __init__(self, entries):
        self._entries = entries

    def peek_session_id(self, key):
        entry = self._entries.get(key)
        return entry.session_id if entry else None


def attach(adapter, source, *, runner=None, run_current=lambda: True):
    agent = SimpleNamespace(session_id="session-1")
    ctx = SimpleNamespace(source=source, session_key="slack-thread", _status_adapter=adapter,
                          _loop_for_step=asyncio.get_running_loop(), _run_still_current=run_current)
    if runner is None:
        runner = SimpleNamespace(session_store=Store(
            {ctx.session_key: SimpleNamespace(session_id=agent.session_id)}))
    holder = SimpleNamespace(_runner=runner)
    TurnRunner._attach_session_title_callback(holder, agent, ctx)
    return agent


@pytest.mark.asyncio
async def test_generated_upgrade_replaces_placeholder_once(adapter, source, monkeypatch):
    from plugins.platforms.slack import adapter as module
    rename = AsyncMock()
    monkeypatch.setattr(module, "_session_title_method", lambda client: rename)
    await adapter._set_assistant_thread_title("D123", source.thread_id, "I agree with this, extract insights", team_id="T123")
    agent = attach(adapter, source)
    assert agent._title_style == "verb_first"
    done = asyncio.Event()
    async def upgraded(**kwargs):
        done.set()
    rename.side_effect = upgraded
    agent._on_session_title("Review account renewal options", "derived")
    assert rename.await_count == 1
    agent._on_session_title("Review account renewal options", "llm")
    await asyncio.wait_for(done.wait(), timeout=5)
    rename.assert_awaited_with(channel_id="D123", thread_ts=source.thread_id, title="Review account renewal options")
    await adapter._upgrade_assistant_thread_title(source, "Clobber manual rename", lambda: True)
    assert rename.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["follow_up", "reset", "reload", "removed"])
async def test_title_callback_fences_session_not_turn(adapter, source, monkeypatch, change):
    from plugins.platforms.slack import adapter as module
    rename = AsyncMock()
    monkeypatch.setattr(module, "_session_title_method", lambda client: rename)
    runner = SimpleNamespace(session_store=Store(
        {"slack-thread": SimpleNamespace(session_id="session-1")}))
    run = {"current": True}
    agent = attach(adapter, source, runner=runner, run_current=lambda: run["current"])
    run["current"] = False
    if change == "reset":
        runner.session_store._entries["slack-thread"].session_id = "session-2"
    elif change == "reload":
        runner.session_store = Store({"slack-thread": SimpleNamespace(session_id="session-2")})
    elif change == "removed":
        runner.session_store._entries.clear()
    completed = asyncio.Event()
    upgrade = adapter._upgrade_assistant_thread_title

    async def observed(*args):
        await upgrade(*args)
        completed.set()

    monkeypatch.setattr(adapter, "_upgrade_assistant_thread_title", observed)
    agent._on_session_title("Review account renewal options", "llm")
    await asyncio.wait_for(completed.wait(), timeout=5)
    assert rename.await_count == int(change == "follow_up")


@pytest.mark.asyncio
async def test_api_failure_is_silent_and_not_retried(adapter, source, monkeypatch):
    from plugins.platforms.slack import adapter as module
    rename = AsyncMock(side_effect=RuntimeError("unavailable"))
    monkeypatch.setattr(module, "_session_title_method", lambda client: rename)
    await adapter._upgrade_assistant_thread_title(source, "Review renewal options", lambda: True)
    await adapter._upgrade_assistant_thread_title(source, "Review renewal options", lambda: True)
    assert rename.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled,stale", [(True, False), (False, True)])
async def test_disabled_or_stale_upgrade_is_skipped(adapter, source, disabled, stale, monkeypatch):
    from plugins.platforms.slack import adapter as module
    rename = AsyncMock()
    monkeypatch.setattr(module, "_session_title_method", lambda client: rename)
    adapter.config.extra["assistant_thread_titles"] = not disabled
    await adapter._upgrade_assistant_thread_title(source, "Review renewal options", lambda: not stale)
    rename.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_failure_keeps_placeholder(adapter, source, monkeypatch):
    from plugins.platforms.slack import adapter as module
    rename = AsyncMock()
    monkeypatch.setattr(module, "_session_title_method", lambda client: rename)
    await adapter._set_assistant_thread_title("D123", source.thread_id, "Review renewal options", team_id="T123")
    agent = attach(adapter, source)
    agent._on_session_title("Review renewal options", "derived")
    assert rename.await_count == 1


@pytest.mark.asyncio
async def test_upgrade_guard_is_workspace_scoped_and_bounded(adapter, source, monkeypatch):
    from plugins.platforms.slack import adapter as module
    rename = AsyncMock()
    monkeypatch.setattr(module, "_session_title_method", lambda client: rename)
    adapter._TITLED_ASSISTANT_THREADS_MAX = 4
    for index in range(8):
        source.thread_id = f"171234.{index:04d}"
        await adapter._upgrade_assistant_thread_title(source, "Review renewal options", lambda: True)
    assert len(adapter._upgraded_assistant_threads) <= 4
    source.scope_id = "T-other"
    await adapter._upgrade_assistant_thread_title(source, "Review renewal options", lambda: True)
    assert rename.await_count == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("placeholder,title,expected", [
    ("I agree, review renewal options", "Review renewal options",
     ["I agree, review renewal options", "Review renewal options"]),
    ("Review Brian’s renewal options", "Compare Brian’s renewal options",
     ["Review Brians renewal options", "Compare Brians renewal options"]),
])
async def test_generated_title_reaches_slack_http(adapter, source, monkeypatch, placeholder, title, expected):
    from slack_sdk.web.async_client import AsyncWebClient
    from plugins.platforms.slack import adapter as module
    from tests.fakes.platforms.slack_standin import SlackStandin

    server = SlackStandin()
    server.start()
    try:
        client = AsyncWebClient(token=server.bot_token, base_url=server.api_base)
        adapter._get_client = lambda *args, **kwargs: client
        monkeypatch.setattr(module, "_sdk_supports_agent_sessions", lambda: False)
        await adapter._set_assistant_thread_title("D123", source.thread_id, placeholder, team_id="T123")
        await adapter._upgrade_assistant_thread_title(source, title, lambda: True)
        calls = server.calls_of("assistant.threads.setTitle")
        assert [call.params["title"] for call in calls] == expected
        assert all(call.response == {"ok": True} for call in calls)
    finally:
        server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "not_authorized", "internal_error", "missing_scope"])
async def test_agent_session_api_falls_back_only_on_known_rejection(adapter, source, monkeypatch, error):
    from slack_sdk.web.async_client import AsyncWebClient
    from plugins.platforms.slack import adapter as module
    from tests.fakes.platforms.slack_standin import SlackStandin

    server = SlackStandin()
    server.start()
    try:
        client = AsyncWebClient(token=server.bot_token, base_url=server.api_base)
        adapter._get_client = lambda *args, **kwargs: client
        monkeypatch.setattr(module, "_sdk_supports_agent_sessions", lambda: True)
        response = {"ok": False, "error": error} if error else {"ok": True}
        server.fail("agents.sessions.rename", response)
        await adapter._upgrade_assistant_thread_title(source, "Review Brian’s renewal options", lambda: True)
        await adapter._upgrade_assistant_thread_title(source, "Overwrite manual title", lambda: True)
        primary = server.calls_of("agents.sessions.rename")
        legacy = server.calls_of("assistant.threads.setTitle")
        assert len(primary) == 1
        assert primary[0].params["title"] == "Review Brians renewal options"
        assert len(legacy) == int(error == "not_authorized")
        if legacy:
            assert legacy[0].params == primary[0].params
            assert legacy[0].response == {"ok": True}
    finally:
        server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("root,renamed", [
    ({"agent_session": {"title": "Apply Max Hodak lessons"}}, False),
    ({"assistant_app_thread": {"title": "Remove Cursor and Notion apps"}}, False),
    ({"agent_session": {"status": "active"}}, True),
])
async def test_reply_after_restart_keeps_existing_title(adapter, source, monkeypatch, root, renamed):
    from plugins.platforms.slack import adapter as module
    rename = AsyncMock()
    monkeypatch.setattr(module, "_session_title_method", lambda client: rename)
    adapter._app.client.conversations_replies = AsyncMock(return_value={"messages": [root]})
    await adapter._set_assistant_thread_title(
        "D123", source.thread_id, "Its too long, dont enforce it", team_id="T123", is_reply=True)
    assert rename.await_count == int(renamed)
    await adapter._set_assistant_thread_title(
        "D123", source.thread_id, "Another reply", team_id="T123", is_reply=True)
    assert adapter._app.client.conversations_replies.await_count == 1


@pytest.mark.asyncio
async def test_rename_normalizes_and_falls_back_for_legacy_roots(adapter, source, monkeypatch):
    from slack_sdk.errors import SlackApiError
    from plugins.platforms.slack import adapter as module
    rename = AsyncMock(side_effect=SlackApiError("not_authorized", {"ok": False, "error": "not_authorized"}))
    monkeypatch.setattr(module, "_session_title_method", lambda client: rename)
    adapter._app.client.assistant_threads_setTitle = AsyncMock()
    await adapter._set_assistant_thread_title("D123", source.thread_id, "Fix Brian\u2019s  bedding", team_id="T123")
    rename.assert_awaited_once_with(channel_id="D123", thread_ts=source.thread_id, title="Fix Brians bedding")
    adapter._app.client.assistant_threads_setTitle.assert_awaited_once_with(
        channel_id="D123", thread_ts=source.thread_id, title="Fix Brians bedding")


@pytest.mark.asyncio
async def test_other_rename_errors_do_not_fall_back(adapter, source, monkeypatch):
    from slack_sdk.errors import SlackApiError
    from plugins.platforms.slack import adapter as module
    rename = AsyncMock(side_effect=SlackApiError("invalid_name", {"ok": False, "error": "invalid_name"}))
    monkeypatch.setattr(module, "_session_title_method", lambda client: rename)
    adapter._app.client.assistant_threads_setTitle = AsyncMock()
    await adapter._set_assistant_thread_title("D123", source.thread_id, "Draft reply", team_id="T123")
    adapter._app.client.assistant_threads_setTitle.assert_not_awaited()


SEEDED = "*Hindsight Maintenance*\nMoved from Telegram. Hermes continues here with the same history."
SEEDED_BLOCKS = [{"type": "rich_text", "block_id": "b1", "elements": [{"type": "rich_text_section", "elements": [
    {"type": "text", "text": "Hindsight Maintenance", "style": {"bold": True}},
    {"type": "text", "text": "\nMoved from Telegram. Hermes continues here with the same history."}]}]}]


@pytest.fixture
def channel(adapter):
    adapter._team_bot_user_ids = {"T123": "UBOT"}
    adapter.config.extra.update(api_human_users=["UOWNER"], channel_thread_titles=True)
    adapter._app.client.chat_update = AsyncMock()
    user_client = AsyncMock()
    user_client.auth_test = AsyncMock(return_value={"ok": True, "team_id": "T123", "user_id": "UOWNER"})
    adapter._new_web_client = MagicMock(return_value=user_client)
    return SessionSource(platform=Platform.SLACK, chat_id="C123", chat_type="group",
                         thread_id="171234.0001", scope_id="T123", user_id="UOWNER"), user_client


def with_root(adapter, **root):
    adapter._app.client.conversations_replies = AsyncMock(
        return_value={"messages": [{"ts": "171234.0001", **root}]})


@pytest.mark.asyncio
async def test_channel_thread_gets_verb_first_title_on_its_root(adapter, channel, monkeypatch):
    from plugins.platforms.slack import adapter as module
    source, _ = channel
    rename = AsyncMock()
    monkeypatch.setattr(module, "_session_title_method", lambda client: rename)
    with_root(adapter, user="UBOT", text="Daily brief is ready")
    done = asyncio.Event()
    adapter._app.client.chat_update.side_effect = lambda **kwargs: done.set()
    agent = attach(adapter, source)
    assert agent._title_style == "verb_first"
    agent._on_session_title("Review daily brief", "llm")
    await asyncio.wait_for(done.wait(), timeout=5)
    adapter._app.client.chat_update.assert_awaited_once_with(
        channel="C123", ts="171234.0001", text="*Review daily brief*\nDaily brief is ready")
    await adapter._title_channel_thread_root(source, "Clobber the title", lambda: True)
    assert adapter._app.client.chat_update.await_count == 1
    rename.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("root,token,edited_by", [
    ({"user": "UBOT", "text": "*Hermes updates*\nAll green"}, "xoxp-owner", None),
    ({"user": "UBOT", "text": "Brief", "blocks": [{"type": "header", "text": {"type": "plain_text", "text": "Brief"}}]},
     "xoxp-owner", None),
    ({"user": "UOWNER", "text": "can you fix the *backup* job?"}, "xoxp-owner", None),
    ({"user": "UOWNER", "text": "*Plan*\nmy own notes"}, "xoxp-owner", None),
    ({"user": "UOTHER", "text": SEEDED, "blocks": SEEDED_BLOCKS}, "xoxp-owner", None),
    ({"user": "UOWNER", "text": SEEDED, "blocks": SEEDED_BLOCKS}, None, None),
    ({"user": "UOWNER", "text": SEEDED, "blocks": SEEDED_BLOCKS}, "xoxp-other", None),
    ({"user": "UOWNER", "text": SEEDED, "blocks": SEEDED_BLOCKS, "edited": {"user": "UOWNER", "ts": "171234.0002"}},
     "xoxp-owner", None),
    ({"user": "UOWNER", "text": SEEDED, "blocks": SEEDED_BLOCKS}, "xoxp-owner", "owner"),
])
async def test_channel_root_rewrite_never_touches_free_human_text(adapter, channel, monkeypatch, root, token, edited_by):
    from plugins.platforms.slack import adapter as module
    source, user_client = channel
    monkeypatch.setattr(module, "_get_scoped_secret", lambda name, default=None: token)
    if token == "xoxp-other":
        user_client.auth_test.return_value = {"ok": True, "team_id": "T123", "user_id": "UOTHER"}
    with_root(adapter, **root)
    await adapter._title_channel_thread_root(source, "Check Hindsight backlog", lambda: True)
    adapter._app.client.chat_update.assert_not_awaited()
    if edited_by is None:
        user_client.chat_update.assert_not_awaited()
        return
    blocks = user_client.chat_update.await_args.kwargs.pop("blocks")
    user_client.chat_update.assert_awaited_once_with(
        channel="C123", ts="171234.0001",
        text="*Check Hindsight backlog*\nMoved from Telegram. Hermes continues here with the same history.")
    runs = blocks[0]["elements"][0]["elements"]
    assert runs[0] == {"type": "text", "text": "Check Hindsight backlog", "style": {"bold": True}}
    assert runs[1:] == SEEDED_BLOCKS[0]["elements"][0]["elements"][1:]


@pytest.mark.asyncio
@pytest.mark.parametrize("author", ["owner", "bot"])
async def test_root_edit_echo_never_starts_a_turn(adapter, channel, monkeypatch, author):
    """Slack echoes the edit as message_changed; an unseen root's text edit is otherwise new input."""
    from plugins.platforms.slack import adapter as module
    source, user_client = channel
    monkeypatch.setattr(module, "_get_scoped_secret", lambda name, default=None: "xoxp-owner")
    adapter.config.extra.update(allow_bots="all", free_response_channels=["C123"])
    adapter._bot_user_id = "UBOT"
    root = ({"user": "UOWNER", "bot_id": "B07K85WLG9K", "app_id": "A07KPKY7Q1F", "text": SEEDED,
             "blocks": SEEDED_BLOCKS} if author == "owner"
            else {"user": "UBOT", "bot_id": "BBOT", "app_id": "AHERMES", "text": "Daily brief is ready"})
    with_root(adapter, **root)
    await adapter._title_channel_thread_root(source, "Check Hindsight backlog", lambda: True)
    writer = user_client if author == "owner" else adapter._app.client
    edit = writer.chat_update.await_args.kwargs
    adapter.handle_message = AsyncMock()
    adapter._resolve_user_name = AsyncMock(return_value="Brian")
    echo = {"type": "message", "subtype": "message_changed", "channel": "C123", "channel_type": "channel",
            "team": "T123", "ts": "171234.0009", "event_ts": "171234.0009",
            "previous_message": {**root, "ts": "171234.0001"},
            "message": {**root, "ts": "171234.0001", "text": edit["text"], "blocks": edit.get("blocks"),
                        "edited": {"user": root["user"], "ts": "171234.0009"}}}
    await adapter._handle_slack_message(echo, {"team_id": "T123", "event_id": "Ev0ECHO"})
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_titles_are_off_unless_enabled(adapter, monkeypatch):
    source = SessionSource(platform=Platform.SLACK, chat_id="C123", chat_type="group",
                           thread_id="171234.0001", scope_id="T123", user_id="UOWNER")
    agent = attach(adapter, source)
    assert agent._title_style == "noun"
    assert not hasattr(agent, "_on_session_title")
    adapter.config.extra["channel_thread_titles"] = True
    adapter.config.extra["assistant_thread_titles"] = False
    assert attach(adapter, source)._title_style == "verb_first"


@pytest.mark.asyncio
async def test_seeded_opener_keeps_matching_title(adapter, channel, monkeypatch):
    from plugins.platforms.slack import adapter as module
    source, user_client = channel
    monkeypatch.setattr(module, "_get_scoped_secret", lambda name, default=None: "xoxp-owner")
    with_root(adapter, user="UOWNER", text=SEEDED, blocks=SEEDED_BLOCKS)
    await adapter._title_channel_thread_root(source, "Hindsight Maintenance", lambda: True)
    user_client.auth_test.assert_not_awaited()
    user_client.chat_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_root_with_blocks_gains_title_through_slack_http(adapter, channel):
    from slack_sdk.web.async_client import AsyncWebClient
    from tests.fakes.platforms.slack_standin import SlackStandin, BOT_USER_ID

    source, _ = channel
    server = SlackStandin()
    server.start()
    try:
        client = AsyncWebClient(token=server.bot_token, base_url=server.api_base)
        adapter._get_client = lambda *args, **kwargs: client
        adapter._team_bot_user_ids = {"T123": BOT_USER_ID}
        body = [{"type": "section", "text": {"type": "mrkdwn", "text": "Backups finished & verified"}}]
        posted = await client.chat_postMessage(channel="C123", text="Backups finished & verified", blocks=body)
        source.thread_id = posted["ts"]
        await adapter._title_channel_thread_root(source, "Verify <nightly> backups", lambda: True)
        await adapter._title_channel_thread_root(source, "Verify backups again", lambda: True)
        [update] = server.calls_of("chat.update")
        assert update.params["text"] == "*Verify &lt;nightly&gt; backups*\nBackups finished & verified"
        assert update.params["blocks"] == [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Verify &lt;nightly&gt; backups*"}}, *body]
        assert update.response["ok"] is True
    finally:
        server.stop()
