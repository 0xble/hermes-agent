"""Every Telegram send_*/edit_message call must wait for reconnect too.

test_telegram_send_reconnect_wait.py pinned send()'s contract: a transient
10-20s blip must be tolerated (wait for _bot or a replacement adapter) instead
of failing immediately with retryable=False, which otherwise strands the
reply in the delivery ledger until the next gateway boot.

That fix was never extended past send() — edit_message and every send_*
helper (approval prompts, model/choice pickers, clarify, media sends) still
failed fast. This file pins the shared _await_reconnection_or_delegate()
mechanism (mirroring send_reconnect_wait.py's own scenarios) and then proves
each real call site is actually wired through it with the correct method
name and arguments — not just that the shared helper works in isolation.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._rich_send_disabled = True
    adapter.send_typing = AsyncMock()
    adapter._RECONNECT_WAIT_SECONDS = 0.6
    adapter._RECONNECT_POLL_INTERVAL = 0.05
    return adapter


def _connected_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    return bot


# ---------------------------------------------------------------------------
# The shared mechanism (_await_reconnection_or_delegate), exercised through
# a representative real call site (edit_message) — same scenarios as
# send_reconnect_wait.py, proving the extracted helper behaves identically.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_guard_waits_and_falls_through_when_bot_returns():
    adapter = _make_adapter()
    adapter._bot = None

    async def restore_bot() -> None:
        await asyncio.sleep(0.12)
        adapter._bot = _connected_bot()
        adapter._bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=99))

    asyncio.get_running_loop().create_task(restore_bot())
    result = await adapter.edit_message("123", "1", "hello")

    assert result.success is True
    adapter._bot.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_shared_guard_delegates_to_replacement_installed_mid_wait():
    old = _make_adapter()
    old._bot = None

    live = _make_adapter()
    live._bot = _connected_bot()
    live._bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=99))
    live._RECONNECT_WAIT_SECONDS = 0.01

    runner = MagicMock()
    runner.adapters = {}
    old.gateway_runner = runner

    async def install_replacement() -> None:
        await asyncio.sleep(0.12)
        runner.adapters[old.platform] = live

    asyncio.get_running_loop().create_task(install_replacement())
    result = await old.edit_message("123", "1", "hello")

    assert result.success is True
    live._bot.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_shared_guard_delegates_immediately_when_replacement_already_live():
    old = _make_adapter()
    old._bot = None
    live = _make_adapter()
    live._bot = _connected_bot()
    live._bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=99))
    runner = MagicMock()
    runner.adapters = {old.platform: live}
    old.gateway_runner = runner
    old._wait_for_reconnection = AsyncMock(
        side_effect=AssertionError("must not wait when replacement is already live")
    )

    result = await old.edit_message("123", "1", "hello")

    assert result.success is True
    live._bot.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_shared_guard_timeout_is_retryable_not_connected():
    adapter = _make_adapter()
    adapter._bot = None
    adapter._RECONNECT_WAIT_SECONDS = 0.15
    adapter._RECONNECT_POLL_INTERVAL = 0.05

    result = await adapter.edit_message("123", "1", "hello")

    assert result.success is False
    assert result.error == "Not connected"
    assert result.retryable is True


@pytest.mark.asyncio
async def test_shared_guard_permanent_fatal_fails_immediately_without_wait():
    adapter = _make_adapter()
    adapter._bot = None
    adapter._set_fatal_error("telegram_auth_error", "invalid token", retryable=False)
    adapter._wait_for_reconnection = AsyncMock(
        side_effect=AssertionError("must not wait on permanent fatal")
    )

    result = await adapter.edit_message("123", "1", "hello")

    assert result.success is False
    assert result.error == "Not connected"
    assert result.retryable is False


# ---------------------------------------------------------------------------
# Wiring: every remaining call site must actually invoke the shared guard
# with its own method name and arguments when disconnected, instead of
# failing fast — proven without needing to mock each method's own deep
# success-path business logic.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_update_prompt_wired_through_shared_guard():
    adapter = _make_adapter()
    adapter._bot = None
    sentinel = SendResult(success=True, message_id="mocked")
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=sentinel)

    result = await adapter.send_update_prompt("123", "prompt", "default", session_key="s1", metadata={"m": 1})

    assert result is sentinel
    adapter._await_reconnection_or_delegate.assert_awaited_once_with(
        "send_update_prompt", "123", "prompt", "default", session_key="s1", metadata={"m": 1},
    )


@pytest.mark.asyncio
async def test_send_exec_approval_wired_through_shared_guard():
    adapter = _make_adapter()
    adapter._bot = None
    sentinel = SendResult(success=True, message_id="mocked")
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=sentinel)

    result = await adapter.send_exec_approval("123", "rm -rf /tmp/x", "s1")

    assert result is sentinel
    adapter._await_reconnection_or_delegate.assert_awaited_once_with(
        "send_exec_approval", "123", "rm -rf /tmp/x", "s1",
        description="dangerous command", metadata=None,
        allow_permanent=True, allow_session=True, smart_denied=False,
    )


@pytest.mark.asyncio
async def test_send_slash_confirm_wired_through_shared_guard():
    adapter = _make_adapter()
    adapter._bot = None
    sentinel = SendResult(success=True, message_id="mocked")
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=sentinel)

    result = await adapter.send_slash_confirm("123", "title", "message", "s1", "cid")

    assert result is sentinel
    adapter._await_reconnection_or_delegate.assert_awaited_once_with(
        "send_slash_confirm", "123", "title", "message", "s1", "cid", metadata=None,
    )


@pytest.mark.asyncio
async def test_send_clarify_wired_through_shared_guard():
    adapter = _make_adapter()
    adapter._bot = None
    sentinel = SendResult(success=True, message_id="mocked")
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=sentinel)

    result = await adapter.send_clarify("123", "question?", ["a", "b"], "cid", "s1")

    assert result is sentinel
    adapter._await_reconnection_or_delegate.assert_awaited_once_with(
        "send_clarify", "123", "question?", ["a", "b"], "cid", "s1", metadata=None,
    )
    # The clarify-state write (self._clarify_state[...]) lives inside the try
    # block past this guard — proving the guard returns early confirms it
    # never runs on THIS (disconnected) instance for a delegated call.
    assert "cid" not in adapter._clarify_state


@pytest.mark.asyncio
async def test_send_model_picker_wired_through_shared_guard():
    adapter = _make_adapter()
    adapter._bot = None
    sentinel = SendResult(success=True, message_id="mocked")
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=sentinel)
    on_selected = MagicMock()

    result = await adapter.send_model_picker("123", ["p1"], "m1", "p1", "s1", on_selected)

    assert result is sentinel
    adapter._await_reconnection_or_delegate.assert_awaited_once_with(
        "send_model_picker", "123", ["p1"], "m1", "p1", "s1", on_selected, metadata=None,
    )


@pytest.mark.asyncio
async def test_send_choice_picker_wired_through_shared_guard():
    adapter = _make_adapter()
    adapter._bot = None
    sentinel = SendResult(success=True, message_id="mocked")
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=sentinel)
    on_selected = MagicMock()

    result = await adapter.send_choice_picker("123", "title", [{"value": "a"}], "s1", on_selected)

    assert result is sentinel
    adapter._await_reconnection_or_delegate.assert_awaited_once_with(
        "send_choice_picker", "123", "title", [{"value": "a"}], "s1", on_selected, metadata=None,
    )


@pytest.mark.asyncio
async def test_send_voice_wired_through_shared_guard():
    adapter = _make_adapter()
    adapter._bot = None
    sentinel = SendResult(success=True, message_id="mocked")
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=sentinel)

    result = await adapter.send_voice("123", "/tmp/x.ogg")

    assert result is sentinel
    adapter._await_reconnection_or_delegate.assert_awaited_once_with(
        "send_voice", "123", "/tmp/x.ogg", caption=None, reply_to=None, metadata=None,
    )


@pytest.mark.asyncio
async def test_send_image_file_wired_through_shared_guard():
    adapter = _make_adapter()
    adapter._bot = None
    sentinel = SendResult(success=True, message_id="mocked")
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=sentinel)

    result = await adapter.send_image_file("123", "/tmp/x.png")

    assert result is sentinel
    adapter._await_reconnection_or_delegate.assert_awaited_once_with(
        "send_image_file", "123", "/tmp/x.png", caption=None, reply_to=None, metadata=None,
    )


@pytest.mark.asyncio
async def test_send_document_wired_through_shared_guard():
    adapter = _make_adapter()
    adapter._bot = None
    sentinel = SendResult(success=True, message_id="mocked")
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=sentinel)

    result = await adapter.send_document("123", "/tmp/x.pdf")

    assert result is sentinel
    adapter._await_reconnection_or_delegate.assert_awaited_once_with(
        "send_document", "123", "/tmp/x.pdf", caption=None, file_name=None, reply_to=None, metadata=None,
    )


@pytest.mark.asyncio
async def test_send_video_wired_through_shared_guard():
    adapter = _make_adapter()
    adapter._bot = None
    sentinel = SendResult(success=True, message_id="mocked")
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=sentinel)

    result = await adapter.send_video("123", "/tmp/x.mp4")

    assert result is sentinel
    adapter._await_reconnection_or_delegate.assert_awaited_once_with(
        "send_video", "123", "/tmp/x.mp4", caption=None, reply_to=None, metadata=None,
    )


@pytest.mark.asyncio
async def test_send_image_wired_through_shared_guard():
    adapter = _make_adapter()
    adapter._bot = None
    sentinel = SendResult(success=True, message_id="mocked")
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=sentinel)

    result = await adapter.send_image("123", "https://example.com/x.png")

    assert result is sentinel
    adapter._await_reconnection_or_delegate.assert_awaited_once_with(
        "send_image", "123", "https://example.com/x.png", caption=None, reply_to=None, metadata=None,
    )


@pytest.mark.asyncio
async def test_send_animation_wired_through_shared_guard():
    adapter = _make_adapter()
    adapter._bot = None
    sentinel = SendResult(success=True, message_id="mocked")
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=sentinel)

    result = await adapter.send_animation("123", "https://example.com/x.gif")

    assert result is sentinel
    adapter._await_reconnection_or_delegate.assert_awaited_once_with(
        "send_animation", "123", "https://example.com/x.gif", caption=None, reply_to=None, metadata=None,
    )


@pytest.mark.asyncio
async def test_edit_message_returns_retryable_not_connected_when_reconnect_fails():
    adapter = _make_adapter()
    adapter._bot = None
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=adapter._RECONNECT_FAILED)

    result = await adapter.edit_message("123", "1", "hello")

    assert result.success is False
    assert result.error == "Not connected"
    assert result.retryable is True


# ---------------------------------------------------------------------------
# _send_message_with_thread_fallback: raises RuntimeError instead of
# returning a SendResult, so it needs its own contract-preserving check —
# reached directly by _handle_callback_query, not only through the six
# guarded send_* prompt methods above.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_fallback_wired_through_shared_guard():
    adapter = _make_adapter()
    adapter._bot = None
    sentinel = MagicMock(message_id=7)
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=sentinel)

    result = await adapter._send_message_with_thread_fallback(chat_id="123", text="hi")

    assert result is sentinel
    adapter._await_reconnection_or_delegate.assert_awaited_once_with(
        "_send_message_with_thread_fallback", chat_id="123", text="hi",
    )


@pytest.mark.asyncio
async def test_thread_fallback_raises_not_connected_when_reconnect_fails():
    adapter = _make_adapter()
    adapter._bot = None
    adapter._await_reconnection_or_delegate = AsyncMock(return_value=adapter._RECONNECT_FAILED)

    with pytest.raises(RuntimeError, match="Not connected"):
        await adapter._send_message_with_thread_fallback(chat_id="123", text="hi")


@pytest.mark.asyncio
async def test_thread_fallback_waits_and_succeeds_when_bot_returns():
    adapter = _make_adapter()
    adapter._bot = None

    async def restore_bot() -> None:
        await asyncio.sleep(0.12)
        adapter._bot = _connected_bot()

    asyncio.get_running_loop().create_task(restore_bot())
    result = await adapter._send_message_with_thread_fallback(chat_id="123", text="hi")

    assert result.message_id == 42
    adapter._bot.send_message.assert_awaited()
