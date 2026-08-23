"""Transport-boundary regressions for Telegram paragraph spacing.

HERMES-035 removed an NBSP expansion that made ordinary paragraphs look
excessively tall. The outbound Bot API payload must preserve the author's
paragraph boundary exactly.
"""

from types import SimpleNamespace

import pytest

from tests.gateway.test_telegram_thread_fallback import _make_adapter


@pytest.mark.asyncio
async def test_plain_send_preserves_paragraph_boundary_without_nbsp():
    adapter = _make_adapter()
    calls = []

    async def send_message(**kwargs):
        calls.append(dict(kwargs))
        return SimpleNamespace(message_id=901)

    adapter._bot = SimpleNamespace(send_message=send_message)
    content = "Paragraph 1\n\nParagraph 2"

    result = await adapter.send(chat_id="123", content=content)

    assert result.success is True
    assert len(calls) == 1
    assert calls[0]["text"] == content
    assert "\u00a0" not in calls[0]["text"]
    assert "\n\n\n" not in calls[0]["text"]
