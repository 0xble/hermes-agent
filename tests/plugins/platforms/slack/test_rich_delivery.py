"""Reply extraction, finalized edits and rejection fallbacks for rich syntax."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter

URL = "https://example.com/photo.png"
CONTENT = f"![Full]({URL})\n\nA paragraph\n![thumb:Small]({URL})\n\n-# Note"


@pytest.fixture
def adapter():
    result = SlackAdapter(PlatformConfig(enabled=True, token="fake", extra={"rich_blocks": True}))
    result._app = MagicMock()
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "111.222"}
    client.chat_update.return_value = {"ts": "111.222"}
    result._get_client = MagicMock(return_value=client)
    result.stop_typing = AsyncMock()
    result._running = True
    return result, client


def test_native_images_are_not_uploaded_twice_and_inline_occurrences_remain(adapter):
    slack, _ = adapter
    attachments, cleaned = slack.extract_images(CONTENT + f"\n\nInline ![Again]({URL}) attached.")
    assert attachments == [(URL, "Again")]
    assert "![Full]" in cleaned
    assert "![thumb:Small]" in cleaned
    assert "![Again]" not in cleaned


def test_context_icon_is_preserved(adapter):
    slack, _ = adapter
    text = f"-# ![Source]({URL}) Verified"
    assert slack.extract_images(text) == ([], text)


@pytest.mark.parametrize("extra", [{}, {"rich_blocks": False}, {"rich_blocks": True, "markdown_blocks": True}])
def test_other_modes_keep_legacy_image_attachments(extra):
    slack = SlackAdapter(PlatformConfig(enabled=True, token="fake", extra=extra))
    text = f"![Photo]({URL})"
    assert slack.extract_images(text) == ([(URL, "Photo")], "")


def test_http_and_oversized_block_content_keep_legacy_extraction(adapter):
    slack, _ = adapter
    attachments, _ = slack.extract_images("![HTTP](http://example.com/a.png)")
    assert attachments == [("http://example.com/a.png", "HTTP")]
    attachments, _ = slack.extract_images(CONTENT + "\n\n" + "x" * 40000)
    assert len(attachments) == 2


@pytest.mark.asyncio
async def test_reply_and_final_edit_have_same_native_blocks_and_text(adapter):
    slack, client = adapter
    await slack.send("D1", CONTENT, metadata={"thread_id": "101.202"})
    await slack.edit_message("D1", "111.222", CONTENT, finalize=True)
    sent = client.chat_postMessage.await_args.kwargs
    edited = client.chat_update.await_args.kwargs
    assert [b["type"] for b in sent["blocks"]] == ["image", "section", "context"]
    assert sent["blocks"] == edited["blocks"]
    assert sent["text"] == edited["text"]
    assert "![Full]" in sent["text"]


@pytest.mark.asyncio
async def test_native_block_rejection_keeps_text_and_thread(adapter):
    slack, client = adapter
    class Rejected(Exception):
        response = {"error": "invalid_blocks"}
    client.chat_postMessage.side_effect = [Rejected(), {"ts": "111.222"}]
    result = await slack.send("D1", CONTENT, metadata={"thread_id": "101.202"})
    assert result.success
    first, fallback = [c.kwargs for c in client.chat_postMessage.await_args_list]
    assert first["blocks"]
    assert "blocks" not in fallback
    assert fallback["text"] == first["text"]
    assert fallback["channel"] == first["channel"] == "D1"
    assert fallback["thread_ts"] == first["thread_ts"]
