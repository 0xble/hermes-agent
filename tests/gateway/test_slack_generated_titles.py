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


def attach(adapter, source):
    agent = SimpleNamespace(session_id="session-1")
    ctx = SimpleNamespace(source=source, _status_adapter=adapter,
                          _loop_for_step=asyncio.get_running_loop(), _run_still_current=lambda: True)
    holder = SimpleNamespace(_runner=None)
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
async def test_generated_title_reaches_slack_http(adapter, source, monkeypatch):
    from slack_sdk.web.async_client import AsyncWebClient
    from plugins.platforms.slack import adapter as module
    from tests.fakes.platforms.slack_standin import SlackStandin

    server = SlackStandin()
    server.start()
    try:
        client = AsyncWebClient(token=server.bot_token, base_url=server.api_base)
        adapter._get_client = lambda *args, **kwargs: client
        monkeypatch.setattr(module, "_sdk_supports_agent_sessions", lambda: False)
        await adapter._set_assistant_thread_title("D123", source.thread_id, "I agree, review renewal options", team_id="T123")
        await adapter._upgrade_assistant_thread_title(source, "Review renewal options", lambda: True)
        calls = server.calls_of("assistant.threads.setTitle")
        assert [call.params["title"] for call in calls] == [
            "I agree, review renewal options", "Review renewal options"]
        assert all(call.response == {"ok": True} for call in calls)
    finally:
        server.stop()
