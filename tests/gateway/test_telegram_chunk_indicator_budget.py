"""Final MarkdownV2 chunks fit Telegram's UTF-16 limit at count-width boundaries.

Upstream PR #126100's static reserve covers three-digit indicators only. The
fork also budgets arbitrary count widths, both escaped parentheses, and fence
separation; the fake transport observes the real send path without network I/O.
"""

import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, utf16_len
from plugins.platforms.telegram.adapter import TelegramAdapter, _separate_chunk_indicator_from_fence


@pytest.mark.asyncio
@pytest.mark.parametrize("count,body_units,character", [
    (99, 4069, "x"), (100, 4069, "x"),
    (999, 4067, "x"), (1000, 4067, "x"), (1001, 4067, "x"),
    (1004, 4067, "x"), (1004, 4066, "😀"),
])
async def test_long_single_line_fence_chunks_fit_telegram_limit(count, body_units, character):
    sent: list[str] = []

    async def fake_send_message(text: str, **_kw):
        sent.append(text)
        return SimpleNamespace(message_id=len(sent))

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._rich_send_disabled = True
    adapter._bot = MagicMock()
    adapter._bot.send_message = fake_send_message
    adapter._telegram_chat_outbound_slot_secs = 0
    # Fill the head chunks and leave a small tail. These source lengths bound the
    # indicator to six/seven digits; UTF-16 units, not codepoints, fill the body.
    repeats = (body_units // utf16_len(character)) * (count - 1) + 50
    result = await adapter.send("4242", "```\n" + character * repeats + "\n```")

    assert result.success is True
    oversized = [(i, utf16_len(text)) for i, text in enumerate(sent) if utf16_len(text) > adapter.MAX_MESSAGE_LENGTH]
    assert not oversized, f"{len(oversized)} oversized chunks: {oversized[:3]}"
    assert len(sent) == count
    for index, text in enumerate(sent, 1):
        assert text.endswith(f"```\n\\({index}/{count}\\)")
        body = text.removesuffix(f"\n\\({index}/{count}\\)")
        assert body.startswith("```\n") and body.endswith("\n```")
        assert body.count("```") == 2
    assert sum(text.count(character) for text in sent) == repeats


@pytest.mark.parametrize("limit,count,fenced", [
    (32, 9, False), (64, 100, False), (128, 1000, False), (256, 10000, False),
    (32, 43, True), (64, 94, True), (32, 100_000, False),
])
@pytest.mark.parametrize("len_fn,character", [(len, "x"), (utf16_len, "😀")])
def test_splitter_budgets_actual_indicators_at_arbitrary_limits(limit, count, fenced, len_fn, character):
    content = ("```\n" if fenced else "") + character * count
    chunks = BasePlatformAdapter.truncate_message(content, limit, len_fn=len_fn, indicator_overhead=TelegramAdapter.CHUNK_INDICATOR_OVERHEAD)
    assert len(chunks) <= len(content)
    if count == 100_000:
        assert len(chunks) >= 10_000  # five-digit counts without megabyte transport fixtures
    bodies = []
    for index, chunk in enumerate(chunks, 1):
        escaped = re.sub(r" \((\d+)/(\d+)\)$", r" \\(\1/\2\\)", chunk)
        payload = _separate_chunk_indicator_from_fence(escaped)
        assert len_fn(payload) <= limit
        if len(chunks) > 1:
            assert chunk.endswith(f" ({index}/{len(chunks)})")
        body = re.sub(r" \(\d+/\d+\)$", "", chunk)
        if fenced:
            assert body.startswith("```\n") and body.endswith("\n```")
            body = body.removeprefix("```\n").removesuffix("\n```")
        bodies.append(body)
    assert "".join(bodies) == character * count


def test_plain_text_does_not_reserve_a_fence_that_is_not_needed():
    content = "x" * 8155
    chunks = BasePlatformAdapter.truncate_message(content, 4096, len_fn=utf16_len, indicator_overhead=TelegramAdapter.CHUNK_INDICATOR_OVERHEAD)

    assert len(chunks) == 2
    assert "".join(re.sub(r" \(\d+/\d+\)$", "", chunk) for chunk in chunks) == content
    for chunk in chunks:
        assert utf16_len(chunk) + 2 <= 4096


def test_splitter_preserves_approved_chunk_boundaries():
    plain_8150 = BasePlatformAdapter.truncate_message("x" * 8150, 4096, len_fn=utf16_len, indicator_overhead=TelegramAdapter.CHUNK_INDICATOR_OVERHEAD)
    plain_8155 = BasePlatformAdapter.truncate_message("x" * 8155, 4096, len_fn=utf16_len, indicator_overhead=TelegramAdapter.CHUNK_INDICATOR_OVERHEAD)
    plain_20000 = BasePlatformAdapter.truncate_message("x" * 20000, 4096, len_fn=utf16_len, indicator_overhead=TelegramAdapter.CHUNK_INDICATOR_OVERHEAD)
    open_fence = BasePlatformAdapter.truncate_message(
        "```python\n" + "x" * 8128, 4096, len_fn=utf16_len, indicator_overhead=TelegramAdapter.CHUNK_INDICATOR_OVERHEAD)

    assert [len(chunk) for chunk in plain_8150] == [4083, 4079]
    assert len(plain_8155) == 2
    assert len(plain_20000) == 5
    assert [len(chunk) for chunk in open_fence] == [4087, 4081]
    for chunk in open_fence:
        body = re.sub(r" \(\d+/\d+\)$", "", chunk)
        assert body.startswith("```python\n")
        assert body.endswith("\n```")
        assert body.count("```") == 2


def test_fence_state_scan_is_bounded_for_long_fenced_input(monkeypatch):
    import gateway.platforms.base as base_module

    scanned = 0
    original = base_module.fence_state_after

    def counting_fence_state(text, *args):
        nonlocal scanned
        scanned += len(text)
        return original(text, *args)

    monkeypatch.setattr(base_module, "fence_state_after", counting_fence_state)
    content = "```python\n" + "x" * 100_000
    chunks = BasePlatformAdapter.truncate_message(content, 4096, len_fn=utf16_len)

    assert len(chunks) > 1
    assert scanned <= 3 * len(content)


def test_final_open_fence_is_closed_within_the_utf16_budget():
    content = "```python\n" + "x" * 185
    chunks = BasePlatformAdapter.truncate_message(content, 64, len_fn=utf16_len, indicator_overhead=TelegramAdapter.CHUNK_INDICATOR_OVERHEAD)

    assert len(chunks) == 5
    for index, chunk in enumerate(chunks, 1):
        body = re.sub(r" \(\d+/\d+\)$", "", chunk)
        assert body.startswith("```python\n")
        assert body.endswith("\n```")
        assert body.count("```") == 2
        assert utf16_len(chunk) + 2 <= 64
