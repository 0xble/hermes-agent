"""Cron delivery to a named Telegram private-DM topic (``telegram:<chat>:<topic name>``, #80483).

Drives the real ``_deliver_result`` and the real ``DeliveryRouter`` over a recording Telegram
adapter: the name must reach ``ensure_dm_topic`` and every send (text and media) must carry the
resolved thread id, never the raw name or the General lane. Owned topics fail closed when lost.
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler import _deliver_result
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult


class _RecordingTelegram:
    platform = Platform.TELEGRAM

    def __init__(self, missing):
        self.calls, self.thread = [], "38049"
        self.missing = missing

    async def ensure_dm_topic(self, chat_id, name, force_create=False, *, owner_key=None, allow_parallel=False):
        self.calls.append(("ensure_dm_topic", chat_id, name, force_create, owner_key, allow_parallel))
        if force_create:
            self.thread = "38050"
        return self.thread

    def mark_telegram_topic_owner_unresolved(self, chat_id, owner_key, *, reason):
        self.calls.append(("unresolved", chat_id, owner_key, reason))

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append(("send", chat_id, (metadata or {}).get("thread_id")))
        return SendResult(success=True, message_id="1")

    async def send_image_file(self, chat_id, image_path, metadata=None, **_kw):
        thread = (metadata or {}).get("thread_id")
        self.calls.append(("send_image_file", chat_id, thread))
        if self.missing and thread == "38049":  # the topic was deleted after the text went out
            return SendResult(success=False, error="Bad Request: message thread not found")
        return SendResult(success=True, message_id="2")


@pytest.mark.parametrize("missing,parallel", [(False, False), (False, True), (True, False), (True, True)])
def test_named_dm_topic_resolves_through_live_adapter_for_text_and_media(tmp_path, missing, parallel):
    image = tmp_path / "chart.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    adapter = _RecordingTelegram(missing)
    config = MagicMock()
    config.platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True)}
    config.get_home_channel = lambda _p: None
    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    try:
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
             patch("gateway.platforms.base.BasePlatformAdapter.filter_media_delivery_paths",
                   side_effect=lambda files: files):
            error = _deliver_result(
                {"id": "j1", "name": "brief", "deliver": "telegram:722341991:Debug",
                 "telegram_topic_allow_parallel": parallel},
                f"Nightly brief\nMEDIA:{image}", adapters={Platform.TELEGRAM: adapter}, loop=loop)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        worker.join(5)

    if missing:
        assert error and "thread not found" in error
        assert ("unresolved", "722341991", "cron:j1", "remote_topic_unavailable") in adapter.calls
    else:
        assert error is None
    sends = [call for call in adapter.calls if call[0] in {"send", "send_image_file"}]
    expected = [("send", "722341991", "38049"), ("send_image_file", "722341991", "38049")]
    assert sends == expected
    resolutions = [call for call in adapter.calls if call[0] == "ensure_dm_topic"]
    assert resolutions and all(call[4:] == ("cron:j1", parallel) for call in resolutions)
    assert not any(call[3] for call in resolutions)
