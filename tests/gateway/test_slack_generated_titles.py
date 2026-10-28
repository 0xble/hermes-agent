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
