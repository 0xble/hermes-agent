"""A final reply with attachments keeps its crash-recovery marker until the attachments are sent.

The delivery ledger records the final's text only. Releasing the turn marker as soon as that text
row exists left a window where a crash lost the attachments: no marker to resume the turn, and a
ledger row that redelivers text alone.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import BasePlatformAdapter, Platform, PlatformConfig, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="m1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


async def _run(reply: str) -> list:
    adapter = _Adapter()
    order: list = []

    async def _release(event):
        order.append("release")

    async def _attachments(*args, **kwargs):
        order.append("attachments")

    adapter._record_delivery_obligation = AsyncMock(return_value="obligation-1")
    adapter._finalize_delivery_obligation = AsyncMock()
    adapter._release_turn_marker = _release
    adapter._deliver_attachments = _attachments
    adapter._start_typing_refresh = lambda *a, **k: None

    async def _handler(evt):
        return reply

    adapter.set_message_handler(_handler)
    event = MessageEvent(
        text="hi", message_id="msg-1", message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="77", user_id="u-1"),
    )
    await asyncio.wait_for(
        adapter._process_message_background(event, "agent:main:telegram:private:77"), timeout=10)
    return order


@pytest.mark.asyncio
async def test_marker_outlives_the_text_until_attachments_are_sent():
    order = await _run("Done. ![chart](https://example.invalid/chart.png)")
    assert "attachments" in order and "release" in order
    assert order.index("release") > order.index("attachments")


@pytest.mark.asyncio
async def test_text_only_final_still_releases_once_ledgered():
    order = await _run("Done, nothing attached.")
    assert order[0] == "release"
