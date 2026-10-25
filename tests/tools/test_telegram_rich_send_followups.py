"""Standalone Telegram rich delivery preserves bounded retries and config parity."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("telegram")

from telegram.error import RetryAfter

from plugins.platforms.telegram.adapter import TelegramAdapter, _FLOOD_INLINE_WAIT_CAP_SECS, _standalone_send
from tools.send_message_senders import _send_telegram


@pytest.fixture
def bot(monkeypatch):
    bot = SimpleNamespace(
        do_api_request=AsyncMock(return_value={"message_id": 123}),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
    )
    monkeypatch.setattr("tools.send_message_senders._telegram_bot", lambda _token: bot)
    return bot


@pytest.mark.parametrize("wait", [1, timedelta(seconds=_FLOOD_INLINE_WAIT_CAP_SECS)])
@pytest.mark.parametrize("repeat", [False, True])
def test_short_retry_after_waits_and_retries_only_once(bot, monkeypatch, wait, repeat):
    sleep = AsyncMock()
    monkeypatch.setattr("tools.send_message_senders.asyncio.sleep", sleep)
    error = RetryAfter(wait)
    bot.do_api_request.side_effect = [error, error if repeat else {"message_id": 456}]

    result = asyncio.run(_send_telegram("test-token", "123", "**hello**", rich_messages="always"))

    assert bot.do_api_request.await_count == 2
    assert bot.do_api_request.await_args_list[0] == bot.do_api_request.await_args_list[1]
    expected = wait.total_seconds() if isinstance(wait, timedelta) else float(wait)
    sleep.assert_awaited_once_with(expected)
    bot.send_message.assert_not_awaited()
    if repeat:
        assert "error" in result
    else:
        assert result["success"] and result["message_id"] == "456"


@pytest.mark.parametrize("error", [
    RetryAfter(_FLOOD_INLINE_WAIT_CAP_SECS + 1),
    TimeoutError("timed out"),
    RuntimeError("502 Bad Gateway"),
    RuntimeError("unknown failure"),
])
def test_long_penalty_and_ambiguous_errors_never_resend(bot, monkeypatch, error):
    sleep = AsyncMock()
    monkeypatch.setattr("tools.send_message_senders.asyncio.sleep", sleep)
    bot.do_api_request.side_effect = error

    result = asyncio.run(_send_telegram("test-token", "123", "**hello**", rich_messages="always"))

    assert "error" in result
    bot.do_api_request.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    sleep.assert_not_awaited()


@pytest.mark.parametrize("failure", ["construction", "eligibility"])
@pytest.mark.parametrize("error", [AttributeError("missing rich state"), TypeError("incompatible rich helper")])
def test_rich_adapter_setup_errors_warn_and_fall_back(bot, monkeypatch, caplog, failure, error):
    if failure == "construction":
        original_new = TelegramAdapter.__new__
        calls = []

        class BrokenAdapter(TelegramAdapter):
            def __new__(cls):
                calls.append(cls)
                if len(calls) == 1:
                    raise error
                return original_new(cls)

        monkeypatch.setattr("plugins.platforms.telegram.adapter.TelegramAdapter", BrokenAdapter)
    else:
        def eligibility(_self, _message):
            raise error

        monkeypatch.setattr(TelegramAdapter, "_should_attempt_rich", eligibility)

    result = asyncio.run(_send_telegram("test-token", "123", "**hello**", rich_messages="always"))

    assert result["success"] and result["message_id"] == "1"
    bot.do_api_request.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["parse_mode"] == "MarkdownV2"
    assert "falling back to legacy" in caplog.text


@pytest.mark.parametrize("extra,rich", [
    ({"rich_messages": "always", "allow_cjk_rich_messages": True, "disable_link_previews": True}, True),
    ({"rich_messages": "always", "allow_cjk_rich_messages": "false"}, False),
    ({"rich_messages": "never", "allow_cjk_rich_messages": True}, False),
    ({}, False),
])
def test_registry_standalone_sender_honors_rich_and_cjk_config(bot, extra, rich):
    result = asyncio.run(_standalone_send(
        SimpleNamespace(token="test-token", extra=extra), "123", "中文", thread_id="234"))

    assert result["success"]
    assert bot.do_api_request.await_count == int(rich)
    assert bot.send_message.await_count == int(not rich)
    if rich:
        payload = bot.do_api_request.await_args.kwargs["api_kwargs"]
        assert payload["message_thread_id"] == 234
        assert payload["link_preview_options"] == {"is_disabled": True}
