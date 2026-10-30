"""Fork coverage for Slack native Stop beyond upstream's contract test: Assistant manifests,
malformed and gated events, and a real gateway turn interrupted through the adapter."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_cli.slack_cli import _build_full_manifest
from plugins.platforms.slack.adapter_native_stop import handle_agent_session_stopped


_EVENT = {"type": "agent_session_stopped", "channel": "C1", "user": "U1",
          "thread_ts": "1700.0001", "streaming_message_ts": [], "event_ts": "1700.0009"}


@pytest.mark.parametrize("experience", ["assistant", "agent", "none"])
def test_manifest_subscribes_to_native_stop_for_session_capable_apps(experience):
    manifest = _build_full_manifest("Hermes", "Your Hermes agent on Slack", messaging_experience=experience)
    events = manifest["settings"]["event_subscriptions"]["bot_events"]
    assert ("agent_session_stopped" in events) is (experience != "none")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["channel", "thread_ts", "user"])
async def test_native_stop_ignores_incomplete_events(missing):
    adapter = SimpleNamespace(_handle_slash_command=AsyncMock())
    event = dict(_EVENT)
    event.pop(missing)
    await handle_agent_session_stopped(adapter, event)
    adapter._handle_slash_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_stop_respects_channel_gate():
    adapter = SimpleNamespace(
        _event_team_id=lambda *_: "T1", _slash_channel_gated=lambda _: True,
        _handle_slash_command=AsyncMock(), stop_typing=AsyncMock(),
    )
    await handle_agent_session_stopped(adapter, _EVENT)
    adapter._handle_slash_command.assert_not_awaited()
    adapter.stop_typing.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_stop_interrupts_real_gateway_turn(monkeypatch):
    from unittest.mock import Mock

    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from plugins.platforms.slack.adapter import SlackAdapter

    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U1")
    config = PlatformConfig(enabled=True, token="xoxb-test")
    adapter = SlackAdapter(config)
    runner = GatewayRunner(config=GatewayConfig(platforms={Platform.SLACK: config}))
    runner.adapters = {Platform.SLACK: adapter}
    adapter.gateway_runner = runner
    adapter.set_message_handler(runner._handle_message)
    adapter.get_chat_info = AsyncMock(return_value={"name": "test", "type": "dm"})
    adapter._get_client = lambda *_args, **_kwargs: SimpleNamespace(chat_postMessage=AsyncMock())
    adapter.send = AsyncMock()
    adapter.stop_typing = AsyncMock()
    source = adapter.build_source(
        chat_id="D1", chat_type="dm", user_id="U1", thread_id=_EVENT["thread_ts"], scope_id="T1",
    )
    original = MessageEvent(text="work", message_type=MessageType.TEXT, source=source)
    key = adapter._event_session_key(original)
    await runner.async_session_store.get_or_create_session(source)
    agent = SimpleNamespace(interrupt=Mock(), session_id="native-stop-test")
    runner._running_agents[key] = agent
    adapter._active_sessions[key] = asyncio.Event()

    await handle_agent_session_stopped(adapter, {**_EVENT, "channel": "D1"}, {"team_id": "T1"})

    agent.interrupt.assert_called_once()
    assert key not in runner._running_agents
    assert key not in adapter._pending_messages
    assert runner._peek_session_state(key).conversation.stop_latched
