"""Smoke tests for gateway /busy command dispatch."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, build_session_key
from hermes_cli.commands import resolve_command


def _make_runner(busy_mode="interrupt"):
    """Create a GatewayRunner with known busy mode."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.session_store = None
    runner.config = None
    runner._busy_input_mode = busy_mode
    return runner


def _make_event(text: str, chat_id: str = "chat-test") -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=f"user-{chat_id}",
        chat_id=chat_id,
        user_name="tester",
        chat_type="dm",
    )
    return MessageEvent(text=text, source=source)


class TestBusyCommand:
    """Test /busy command dispatch without config persistence."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("command", "busy_mode"),
        [("/busy status", "queue"), ("/busy", "steer")],
    )
    async def test_status_returns_current_mode(self, command, busy_mode):
        """Bare /busy and /busy status show the current busy mode."""
        runner = _make_runner(busy_mode=busy_mode)
        event = _make_event(command)
        result = await runner._handle_busy_command(event)
        reply_text = str(result).lower()
        assert busy_mode in reply_text
        assert "busy" in reply_text


class TestBusyCommandPersistence:
    """Test /busy persistence with mocked save_config_value."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("initial_mode", "new_mode"),
        [
            ("interrupt", "queue"),
            ("queue", "steer"),
            ("queue", "interrupt"),
        ],
    )
    async def test_set_mode_persists(self, monkeypatch, initial_mode, new_mode):
        """Each supported /busy mode is saved and applied."""
        runner = _make_runner(busy_mode=initial_mode)
        runner._busy_text_mode = "interrupt"
        monkeypatch.setattr("cli.save_config_value", lambda k, v: True)
        # The handler re-derives _busy_text_mode from the saved config;
        # emulate the write that the mocked save_config_value skipped.
        monkeypatch.setattr(
            gateway_run,
            "_load_gateway_config",
            lambda: {"display": {"busy_input_mode": new_mode}},
        )
        monkeypatch.delenv("HERMES_GATEWAY_BUSY_TEXT_MODE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_BUSY_INPUT_MODE", raising=False)
        event = _make_event(f"/busy {new_mode}")
        result = await runner._handle_busy_command(event)
        assert new_mode in str(result).lower()
        assert runner._busy_input_mode == new_mode
        # busy_input_mode is the source of truth for the text mode: /busy
        # queue must stop live text messages from interrupting (#97932).
        assert runner._busy_text_mode == (
            "queue" if new_mode == "queue" else "interrupt"
        )

    @pytest.mark.asyncio
    async def test_save_failure_preserves_mode(self, monkeypatch):
        """When save_config_value returns False, mode is unchanged."""
        runner = _make_runner(busy_mode="steer")
        monkeypatch.setattr(
            "cli.save_config_value", lambda k, v: False
        )
        event = _make_event("/busy queue")
        result = await runner._handle_busy_command(event)
        assert "unchanged" in str(result).lower()
        assert runner._busy_input_mode == "steer"


def test_deferred_policy_registry_covers_session_mutations():
    for name in ("compress", "undo", "retry", "save", "branch", "moa"):
        assert resolve_command(name).busy_policy == "defer_until_idle"
    for name in ("fast", "reasoning", "title"):
        assert resolve_command(name).busy_policy == "dispatch"
    assert resolve_command("model").busy_policy == "defer_until_idle"


@pytest.mark.asyncio
async def test_model_busy_command_is_deferred_and_replayed_once():
    """A model pick made during a turn runs after the current turn, never against its live agent."""
    from gateway.platforms.base import BasePlatformAdapter

    class _Adapter(BasePlatformAdapter):
        async def connect(self, *, is_reconnect=False): pass
        async def disconnect(self): pass
        async def send(self, *args, **kwargs): pass
        async def get_chat_info(self, *args, **kwargs): return {}

    adapter = _Adapter(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda _source: adapter
    turns = []
    busy = True

    async def handler(event):
        if busy:
            return await runner._dispatch_busy_slash_command(
                event, resolve_command("model"), build_session_key(event.source), event.source)
        turns.append(event.text)

    adapter.set_message_handler(handler)
    event = _make_event("/model sonnet")
    session_key = build_session_key(event.source)
    await adapter.handle_message(event)
    await asyncio.sleep(0.2)

    assert turns == []
    assert adapter._deferred_commands[session_key][0][2].text == "/model sonnet"

    busy = False
    adapter.resume_deferred_commands(session_key)
    await asyncio.sleep(0.1)
    await adapter.cancel_background_tasks()
    assert turns == ["/model sonnet"]


@pytest.mark.asyncio
async def test_moa_busy_command_is_deferred_and_replayed_once():
    """Gateway busy dispatch keeps /moa as a command and runs it only after release."""
    from gateway.platforms.base import BasePlatformAdapter
    from hermes_cli.commands import resolve_command

    class _Adapter(BasePlatformAdapter):
        async def connect(self, *, is_reconnect=False): pass
        async def disconnect(self): pass
        async def send(self, *args, **kwargs): pass
        async def get_chat_info(self, *args, **kwargs): return {}

    adapter = _Adapter(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)
    sent = []
    adapter._send_with_retry = AsyncMock(side_effect=lambda **kwargs: sent.append(kwargs.get("content")))
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda _source: adapter
    busy = True
    turns = []

    async def handler(event):
        if busy:
            return await runner._dispatch_busy_slash_command(
                event, resolve_command("moa"), build_session_key(event.source), event.source)
        turns.append(event.text)
        return None

    adapter.set_message_handler(handler)
    event = _make_event("/moa compare these answers")
    session_key = build_session_key(event.source)
    await adapter.handle_message(event)
    await asyncio.sleep(0.2)
    assert turns == []
    assert len([text for text in sent if text and "scheduled" in text]) == 1

    busy = False
    adapter.resume_deferred_commands(session_key)
    await asyncio.sleep(0.1)
    await adapter.cancel_background_tasks()
    assert turns == ["/moa compare these answers"]
    assert len([text for text in sent if text and "scheduled" in text]) == 1


@pytest.mark.asyncio
async def test_empty_moa_busy_command_returns_usage_without_queue(monkeypatch):
    from hermes_cli.commands import resolve_command
    runner = _make_runner()
    adapter = SimpleNamespace(defer_command_until_idle=lambda *_args: pytest.fail("must not queue"))
    runner._delivery_adapter_for = lambda _source: adapter
    monkeypatch.setattr("hermes_cli.moa_config.moa_usage", lambda: "usage: /moa <prompt>")
    event = _make_event("/moa")
    result = await runner._dispatch_busy_slash_command(
        event, resolve_command("moa"), "session", event.source)
    assert result == "usage: /moa <prompt>"
