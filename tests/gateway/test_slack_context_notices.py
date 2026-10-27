"""Explicit notice presentation is independent of prose and foreground replies."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.run import GatewayRunner

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


@pytest.fixture
def adapter():
    result = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    result._app = MagicMock()
    result._running = True
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "1.2"}
    client.chat_update.return_value = {"ts": "1.2"}
    result._get_client = MagicMock(return_value=client)
    result.stop_typing = AsyncMock()
    return result, client


@pytest.mark.asyncio
async def test_notice_post_and_status_edit_preserve_context(adapter):
    slack, client = adapter
    metadata = {"thread_id": "1.0", "notice_style": "context"}
    content = "✓ **Goal set:** Read `state` and [proof](https://example.com)"
    await slack.send_or_update_status("C1", "receipt", content, metadata=metadata)
    await slack.send_or_update_status("C1", "receipt", content, metadata=metadata)
    expected = [{"type": "context", "elements": [{"type": "mrkdwn", "text": slack.format_message(content)}]}]
    for method in (client.chat_postMessage, client.chat_update):
        payload = method.await_args.kwargs
        assert payload["blocks"] == expected
        assert payload["text"] == "✓ Goal set: Read state and proof"
        assert "notice_style" not in payload
    assert metadata == {"thread_id": "1.0", "notice_style": "context"}


@pytest.mark.asyncio
async def test_context_limits_unmarked_and_foreground_stream(adapter):
    slack, client = adapter
    await slack.send("C1", "✓ **Goal achieved**")
    assert "blocks" not in client.chat_postMessage.await_args.kwargs
    await slack.send("C1", "x" * 3000, metadata={"notice_style": "context"})
    assert client.chat_postMessage.await_args.kwargs["blocks"][0]["type"] == "context"
    await slack.send("C1", "x" * 3001, metadata={"notice_style": "context"})
    assert "blocks" not in client.chat_postMessage.await_args.kwargs
    key = ("", "C1", "1.0")
    stream = {"ts": "1.2", "sent": "same text"}
    slack._active_streams[key] = stream
    assert await slack._try_finalize_stream("C1", "same text", {"thread_id": "1.0", "notice_style": "context"}) is None
    assert slack._active_streams[key] is stream
    client.chat_stopStream.assert_not_awaited()


@pytest.mark.asyncio
async def test_marked_send_does_not_finalize_foreground_stream(adapter):
    slack, client = adapter
    key = ("", "C1", "1.0")
    stream = {"ts": "1.2", "sent": "agent response"}
    slack._active_streams[key] = stream
    result = await slack.send("C1", "FYI", metadata={"thread_id": "1.0", "notice_style": "context"})
    assert result.success
    assert slack._active_streams[key] is stream
    client.chat_stopStream.assert_not_awaited()
    assert client.chat_postMessage.await_args.kwargs["blocks"][0]["type"] == "context"


@pytest.mark.asyncio
async def test_private_notice_and_slash_fallback_use_context(adapter):
    slack, client = adapter
    client.chat_postEphemeral.return_value = {"ok": True, "ts": "1.2"}
    slack._client_for = MagicMock(return_value=client)
    metadata = {"notice_style": "context"}
    assert (await slack.send_private_notice("C1", "U1", "**FYI**", metadata=metadata)).success
    assert client.chat_postEphemeral.await_args.kwargs["text"] == "FYI"
    assert client.chat_postEphemeral.await_args.kwargs["blocks"][0]["type"] == "context"
    assert (await slack._post_ephemeral_fallback("C1", {"user_id": "U1"}, "**FYI**", metadata)).success
    assert client.chat_postEphemeral.await_args.kwargs["text"] == "FYI"
    assert client.chat_postEphemeral.await_args.kwargs["blocks"][0]["type"] == "context"


@pytest.mark.asyncio
async def test_context_and_action_statuses_never_share_a_bubble(adapter):
    slack, client = adapter
    await slack.send_or_update_status("C1", "status", "FYI", metadata={"notice_style": "context"})
    await slack.send_or_update_status("C1", "status", "Please /retry")
    assert client.chat_postMessage.await_count == 2
    client.chat_update.assert_not_awaited()
    assert "blocks" not in client.chat_postMessage.await_args.kwargs


@pytest.mark.parametrize("platform", ["telegram", "discord", "matrix"])
def test_non_slack_metadata_is_identical(platform):
    from agent.notice_style import notice_metadata
    metadata = {"thread_id": "42", "custom": "kept"}
    assert notice_metadata(metadata, platform=platform, style="context") is metadata


def _turn(monkeypatch, platform="slack"):
    import asyncio
    from types import SimpleNamespace
    from gateway import run
    from gateway.config import Platform
    from gateway.run_turn_runner import TurnRunner
    from gateway.session import SessionSource
    from gateway.turn_context import TurnContext
    source = SessionSource(platform=Platform(platform), chat_id="C1", user_id="U1", thread_id="1.0")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)),
                              send_or_update_status=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner = object.__new__(run.GatewayRunner)
    runner.config = None
    runner._delivery_adapter_for = lambda source: adapter
    runner._thread_metadata_for_source = lambda source: {"thread_id": "1.0"}
    ctx = TurnContext(source=source, user_config={"display": {"suppress_warning_notifications": False}},
                      _run_still_current=lambda: True, _status_adapter=adapter,
                      _status_chat_id="C1", _status_thread_metadata={"thread_id": "1.0"})
    turn = TurnRunner(runner, ctx)
    monkeypatch.setattr(run, "safe_schedule_threadsafe", lambda coro, *a, **k: asyncio.run(coro))
    monkeypatch.setattr(run, "_load_gateway_config", lambda: ctx.user_config)
    return turn, runner, adapter


def test_attended_switch_route_is_context(monkeypatch):
    turn, _, adapter = _turn(monkeypatch)
    turn._emit_persistent_attended_notice("Switched route")
    assert adapter.send.await_args.kwargs["metadata"]["notice_style"] == "context"
    assert adapter.send.await_args.kwargs["metadata"]["_interim_send"] is True


@pytest.mark.parametrize("diagnostic", [False, True])
def test_lifecycle_producer_classification_survives_gateway(monkeypatch, diagnostic):
    from agent.status_output import StatusOutputMixin
    turn, _, adapter = _turn(monkeypatch)
    agent = StatusOutputMixin()
    agent.suppress_status_output = True
    agent.status_callback = turn._status_callback_sync
    emit = agent._emit_diagnostic_status if diagnostic else agent._emit_status
    emit("Retrying automatically", context=True)
    assert adapter.send_or_update_status.await_args.kwargs["metadata"]["notice_style"] == "context"
    emit("Please /retry")
    assert "notice_style" not in adapter.send_or_update_status.await_args.kwargs["metadata"]


@pytest.mark.parametrize("name", ["goal_set", "loop_set"])
def test_committed_goal_and_loop_plugin_receipts_reach_context(monkeypatch, name):
    import json
    from agent import inline_tool_executors as ite
    from types import SimpleNamespace
    turn, _, adapter = _turn(monkeypatch)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    monkeypatch.setattr("model_tools._emit_post_tool_call_hook", lambda **kw: None)
    agent = SimpleNamespace(notice_callback=turn._notice_callback_sync, _emit_notice=turn._notice_callback_sync)
    ite.emit_terminal_post_tool_call(agent, function_name=name, function_args={},
                                    result=json.dumps({"success": True, "persisted": True, "notice": "FYI receipt"}),
                                    effective_task_id="task", tool_call_id="call")
    assert adapter.send.await_count == 1
    assert adapter.send.await_args.kwargs["metadata"]["notice_style"] == "context"


def test_unclassified_plugin_notice_stays_full(monkeypatch):
    from agent.credits_tracker import AgentNotice
    turn, _, adapter = _turn(monkeypatch)
    turn._notice_callback_sync(AgentNotice(text="An arbitrary plugin needs a decision", key="plugin.custom"))
    assert adapter.send.await_count == 1
    assert "notice_style" not in adapter.send.await_args.kwargs["metadata"]


def test_credit_usage_is_context_but_depletion_is_full(monkeypatch):
    from agent.credits_tracker import _sticky_notice
    turn, _, adapter = _turn(monkeypatch)
    turn._notice_callback_sync(_sticky_notice("Usage", "warn", "credits.usage", context=True))
    assert adapter.send.await_args.kwargs["metadata"]["notice_style"] == "context"
    turn._notice_callback_sync(_sticky_notice("Please /topup", "error", "credits.depleted"))
    assert "notice_style" not in adapter.send.await_args.kwargs["metadata"]


@pytest.mark.parametrize("status,style", [("active", "context"), ("done", "context"), ("paused", None)])
def test_goal_status_classified_at_producer(status, style):
    from hermes_cli.goals import GoalManager, GoalState
    manager = object.__new__(GoalManager)
    manager._state = GoalState(goal="ship", status=status)
    manager._unreadable = None
    assert getattr(manager.status_line(), "notice_style", None) == style


def test_loop_completion_is_context_but_budget_pause_is_full():
    from hermes_cli.loops import LoopManager, LoopState
    complete = LoopState(prompt="check", times=1, ticks_fired=1)
    paused = LoopState(prompt="check", times=0, max_ticks=1, ticks_fired=1)
    assert getattr(LoopManager._cap_stop(complete)[2], "notice_style", None) == "context"
    assert getattr(LoopManager._cap_stop(paused)[2], "notice_style", None) is None


def test_subagent_failure_stays_full(monkeypatch):
    turn, _, adapter = _turn(monkeypatch)
    turn._progress_subagent_notice("failed", {"status": "failed", "goal": "child task", "failure_notice_claimed": False})
    assert adapter.send.await_count == 1
    assert "notice_style" not in adapter.send.await_args.kwargs["metadata"]


@pytest.mark.asyncio
async def test_restart_resend_stays_full():
    from types import SimpleNamespace
    runner = object.__new__(GatewayRunner)
    runner._resolve_profile_home_for_source = lambda source: None
    runner._deliver_platform_notice = AsyncMock(return_value=True)
    event = SimpleNamespace(source=SimpleNamespace())
    assert await runner._deliver_recovered_followup_note(event, "session", text="Please resend")
    runner._deliver_platform_notice.assert_awaited_once_with(event.source, "Please resend")


@pytest.mark.asyncio
async def test_telegram_send_ignores_notice_style(monkeypatch):
    from agent.notice_style import ContextNotice, notice_metadata
    from plugins.platforms.telegram.adapter import TelegramAdapter
    telegram = TelegramAdapter(PlatformConfig(enabled=True, token="test"))
    telegram._bot = AsyncMock()
    telegram._bot.send_message.return_value = MagicMock(message_id=42)
    telegram._running = True
    telegram.stop_typing = AsyncMock()
    metadata = {"thread_id": "7"}
    assert (await telegram.send("123", "**FYI**", metadata=metadata)).success
    baseline = telegram._bot.send_message.await_args
    assert (await telegram.send("123", ContextNotice("**FYI**"), metadata={**metadata, "notice_style": "context"})).success
    assert telegram._bot.send_message.await_args == baseline


@pytest.mark.asyncio
async def test_background_start_receipt_is_context():
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from gateway.config import Platform
    runner = object.__new__(GatewayRunner)
    source = SessionSource(platform=Platform.SLACK, chat_id="C1")
    runner._background_reply_route = AsyncMock(return_value=(source, None))
    runner._run_background_task = AsyncMock()
    runner._track_background_task = lambda coro: coro.close()
    output = await runner._handle_background_command(MessageEvent(text="/bg check", source=source))
    assert getattr(output, "notice_style", None) == "context"


def test_reasoning_and_fast_change_receipts_are_context_but_invalid_is_full():
    runner = object.__new__(GatewayRunner)
    runner._set_reasoning_override = MagicMock()
    assert getattr(runner._apply_reasoning_selection("s", "slack", "high"), "notice_style", None) == "context"
    assert getattr(runner._apply_reasoning_selection("s", "slack", "invalid"), "notice_style", None) is None
    runner._set_session_service_tier_override = MagicMock()
    runner._apply_live_service_tier = MagicMock()
    runner._fast_route_ignored_note = lambda session_key, tier: ""
    assert getattr(runner._apply_fast_selection("s", "on"), "notice_style", None) == "context"
    assert getattr(runner._apply_fast_selection("s", "invalid"), "notice_style", None) is None


@pytest.mark.asyncio
async def test_personality_success_is_context_but_failed_save_is_full(monkeypatch):
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from gateway.config import Platform
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr("hermes_cli.personality.persist_personality", lambda name: True)
    runner = object.__new__(GatewayRunner)
    event = MessageEvent(text="/personality none", source=SessionSource(platform=Platform.SLACK, chat_id="C1"))
    assert getattr(await runner._handle_personality_command(event), "notice_style", None) == "context"
    monkeypatch.setattr("hermes_cli.personality.persist_personality", lambda name: False)
    assert getattr(await runner._handle_personality_command(event), "notice_style", None) is None


@pytest.mark.asyncio
async def test_context_length_uses_converted_mrkdwn_and_long_edit_clears_blocks(adapter):
    slack, client = adapter
    metadata = {"notice_style": "context"}
    await slack.send_or_update_status("C1", "receipt", "short", metadata=metadata)
    await slack.send_or_update_status("C1", "receipt", "x" * 3001, metadata=metadata)
    assert client.chat_update.await_args.kwargs["blocks"] == []
    blocks = slack._maybe_blocks("&" * 1000, metadata)
    assert not blocks or blocks[0]["type"] != "context"


@pytest.mark.asyncio
async def test_multi_chunk_notice_uses_normal_rendering(adapter):
    slack, client = adapter
    slack.MAX_MESSAGE_LENGTH = 1000
    await slack.send("C1", "x" * 1500, metadata={"notice_style": "context"})
    assert client.chat_postMessage.await_count == 2
    assert all("blocks" not in call.kwargs for call in client.chat_postMessage.await_args_list)


def test_command_marker_preserves_ephemeral_type_and_expiry():
    from agent.notice_style import ContextNotice
    from gateway.platforms.base import CommandReply, EphemeralReply, as_command_reply
    reply = as_command_reply(ContextNotice("FYI"))
    assert isinstance(reply, CommandReply)
    assert reply.notice_style == "context"
    assert as_command_reply(reply) is reply
    ephemeral = EphemeralReply("FYI", ttl_seconds=7)
    ephemeral.notice_style = "context"
    assert as_command_reply(ephemeral) is ephemeral
    assert ephemeral.ttl_seconds == 7
