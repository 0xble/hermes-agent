"""Regression coverage for partial Telegram overflow delivery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.stream_consumer import GatewayStreamConsumer


def _message(message_id: int | str) -> SimpleNamespace:
    return SimpleNamespace(message_id=message_id)


@pytest.fixture
def telegram_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._bot = MagicMock()
    object.__setattr__(adapter, "MAX_MESSAGE_LENGTH", 160)
    return adapter


@pytest.mark.asyncio
async def test_edit_overflow_split_reports_later_partial_failure_after_some_continuations_land(telegram_adapter):
    """Partial metadata tracks the last delivered continuation before failure."""
    content = "word " * 120
    telegram_adapter._bot.edit_message_text = AsyncMock(return_value=True)
    telegram_adapter._bot.send_message = AsyncMock(
        side_effect=[
            _message(202),
            RuntimeError("telegram send failed"),
            RuntimeError("telegram send failed"),
        ]
    )

    result = await telegram_adapter._edit_overflow_split(
        "12345", "201", content, finalize=False, metadata={"thread_id": "77"}
    )

    assert result.success is False
    assert result.message_id == "202"
    assert result.raw_response["partial_overflow"] is True
    assert result.raw_response["delivered_chunks"] == 2
    assert result.raw_response["last_message_id"] == "202"
    assert result.continuation_message_ids == ("202",)




@pytest.mark.asyncio
@pytest.mark.parametrize('business,dm_topic', [(True, True), (False, True), (True, False)])
async def test_final_overflow_missing_anchor_retains_business_identity(telegram_adapter, business, dm_topic):
    metadata = {'thread_id': '77', 'telegram_dm_topic_reply_fallback': dm_topic}
    if business:
        metadata['telegram_business_connection_id'] = 'biz-A'
    telegram_adapter._bot.edit_message_text = AsyncMock(return_value=True)
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError('reply message not found')
        return _message(202 + len(calls))

    telegram_adapter._bot.send_message = AsyncMock(side_effect=send)
    result = await telegram_adapter.edit_message('12345', '201', 'word ' * 60, finalize=True, metadata=metadata)
    assert result.success
    assert len(calls) >= 2
    for call in calls:
        assert call.get('business_connection_id') == ('biz-A' if business else None)
    retry = calls[1]
    assert 'reply_to_message_id' not in retry
    if dm_topic:
        assert 'message_thread_id' not in retry
        assert 'direct_messages_topic_id' not in retry
    else:
        assert retry['message_thread_id'] == 77
    assert telegram_adapter._bot.edit_message_text.await_args.kwargs.get('business_connection_id') == (
        'biz-A' if business else None)
