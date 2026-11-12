"""Slack status preserves free text while titles use the Agent Sessions API."""

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from gateway.config import PlatformConfig
from tests.gateway.test_slack import SlackAdapter, _slack_mod


@pytest.fixture(autouse=True)
def _pin_legacy_assistant_threads_api():
    """Pin the SDK capability probe to the legacy assistant.threads API and
    restore it afterwards, so tests that flip the cached flag on don't leak."""
    prev = _slack_mod._AGENT_SESSIONS_SUPPORTED
    _slack_mod._AGENT_SESSIONS_SUPPORTED = False
    yield
    _slack_mod._AGENT_SESSIONS_SUPPORTED = prev


class TestAgentSessionsApiRouting:
    """slack-sdk 3.44.0 Agent Sessions API (assistant_view deprecation Feb 2027).

    Titles use agents.sessions.rename when available. Status stays on
    assistant.threads.setStatus because free-text phrases and the empty clear
    are not valid Agent Sessions status enum values.
    """

    def _adapter(self):
        config = PlatformConfig(enabled=True, token="xoxb-fake-token")
        a = SlackAdapter(config)
        a._app = MagicMock()
        a._app.client = AsyncMock()
        return a

    @pytest.mark.asyncio
    @pytest.mark.parametrize("supported", [False, True])
    @pytest.mark.parametrize("channel", ["C123", "D123"])
    async def test_typing_keeps_free_text_on_legacy_api(self, supported, channel):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = supported
        a = self._adapter()
        a._app.client.agents_sessions_setStatus = AsyncMock()
        a._app.client.assistant_threads_setStatus = AsyncMock()
        md = {"thread_id": "parent_ts", "slack_team_id": "T1"}
        a.set_status_text(channel, "Hermes Searching...", metadata=md)
        for _ in range(3):
            await a.send_typing(channel, metadata=md)
        assert a._app.client.assistant_threads_setStatus.await_args_list == [
            call(channel_id=channel, thread_ts="parent_ts", status="Hermes Searching...")
        ] * 3
        expected = [call(channel_id=channel, thread_ts="parent_ts", status="processing")] if supported else []
        assert a._app.client.agents_sessions_setStatus.await_args_list == expected


    @pytest.mark.asyncio
    @pytest.mark.parametrize("supported", [False, True])
    async def test_stop_typing_clears_via_legacy_api(self, supported):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = supported
        a = self._adapter()
        a._app.client.agents_sessions_setStatus = AsyncMock()
        a._app.client.assistant_threads_setStatus = AsyncMock()
        await a.send_typing("C123", metadata={"thread_id": "parent_ts"})
        a._app.client.assistant_threads_setStatus.reset_mock()
        await a.stop_typing("C123", metadata={"thread_id": "parent_ts"})
        a._app.client.assistant_threads_setStatus.assert_awaited_once_with(
            channel_id="C123",
            thread_ts="parent_ts",
            status="",
        )
        expected = [call(channel_id="C123", thread_ts="parent_ts", status=value)
                    for value in ("processing", "active")] if supported else []
        assert a._app.client.agents_sessions_setStatus.await_args_list == expected

    @pytest.mark.asyncio
    async def test_thread_title_uses_agents_sessions_rename(self):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        a._app.client.agents_sessions_rename = AsyncMock()
        a._app.client.assistant_threads_setTitle = AsyncMock()
        await a._set_assistant_thread_title("D123", "171234.0001", "Summarize the incident")
        a._app.client.agents_sessions_rename.assert_called_once_with(
            channel_id="D123",
            thread_ts="171234.0001",
            title="Summarize the incident",
        )
        a._app.client.assistant_threads_setTitle.assert_not_called()
