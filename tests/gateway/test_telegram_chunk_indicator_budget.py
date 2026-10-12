"""Every MarkdownV2 chunk of a long split reply fits Telegram's 4096 UTF-16 unit limit.

``truncate_message()`` reserves room for its `` (N/M)`` suffix, but the Telegram send path then
escapes it to `` \\(N/M\\)`` (two units longer) and lifts it off a synthesized closing fence onto
its own line. A single fenced line long enough for 100+ hard splits therefore produced chunks of
4098 units, which Telegram rejects as too long. Same reserve as upstream PR #126100.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import utf16_len
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
async def test_long_single_line_fence_chunks_fit_telegram_limit():
    sent: list[str] = []

    async def fake_send_message(text: str, **_kw):
        sent.append(text)
        return MagicMock(message_id=len(sent))

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._rich_send_disabled = True
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(side_effect=fake_send_message)
    adapter._telegram_chat_outbound_slot_secs = 0

    result = await adapter.send("4242", "```\n" + "x" * 420_000 + "\n```")

    assert result.success is True
    assert len(sent) >= 100  # three-digit indicators: the widest escaped suffix
    assert sent[-1].endswith(f"```\n\\({len(sent)}/{len(sent)}\\)")
    oversized = [(i, utf16_len(text)) for i, text in enumerate(sent) if utf16_len(text) > adapter.MAX_MESSAGE_LENGTH]
    assert not oversized, f"{len(oversized)} chunks over {adapter.MAX_MESSAGE_LENGTH} UTF-16 units: {oversized[:3]}"
