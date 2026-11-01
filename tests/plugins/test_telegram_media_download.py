"""Read-only fetch recovery stays on the live Bot API client and bounds ingress delay."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest

pytest.importorskip("telegram", reason="integration boundary requires the optional telegram extra")

from telegram import Bot, Message
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, TimedOut
from telegram.request import HTTPXRequest

from gateway.config import PlatformConfig
from gateway.platforms.base import ingress_consumer_scope, leave_ingress_consumer
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram import telegram_media_download as media
from tests.gateway.test_telegram_file_retry import _event, _file_obj


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["metadata", "bytes"])
@pytest.mark.parametrize("read_timeout", [20.0, 90.0])
async def test_transient_fetch_recovers_on_same_bot(monkeypatch, failure_stage, read_timeout):
    monkeypatch.setenv("HERMES_TELEGRAM_HTTP_READ_TIMEOUT", str(read_timeout))
    sleeps = AsyncMock()
    monkeypatch.setattr(media.asyncio, "sleep", sleeps)
    calls = []
    failures = 0

    async def respond(request):
        nonlocal failures
        stage = "metadata" if request.url.path.endswith("/getFile") else "bytes"
        calls.append(stage)
        assert request.extensions["timeout"]["read"] == read_timeout * (failures + 1)
        if stage == failure_stage and failures < 2:
            failures += 1
            raise httpx.ReadTimeout("temporary read timeout", request=request)
        if stage == "metadata":
            return httpx.Response(200, json={"ok": True, "result": {
                "file_id": "voice-id", "file_unique_id": "unique", "file_size": 3,
                "file_path": "voice/test.oga"}})
        return httpx.Response(200, content=b"ogg")

    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(respond)})
    bot = Bot(token="123456:fake-test-token", request=request,
              base_url="https://custom.example/bot", base_file_url="https://custom.example/files/")
    source = SimpleNamespace(get_file=lambda **kwargs: bot.get_file("voice-id", **kwargs))
    try:
        fetched, data = await media.fetch_telegram_file(source, "voice message")
        assert data == b"ogg" and fetched.file_id == "voice-id"
        assert calls == (["metadata"] * 3 + ["bytes"] if failure_stage == "metadata"
                         else ["metadata", "bytes"] * 3)
        assert [call.args[0] for call in sleeps.await_args_list] == [2.0, 5.0]
    finally:
        await request.shutdown()
        await bot.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [BadRequest("unavailable"), Forbidden("blocked"), InvalidToken("invalid"),
                                  asyncio.CancelledError(), NetworkError("offline")])
async def test_permanent_errors_cancellation_and_exhausted_retries(monkeypatch, error):
    sleeps = AsyncMock()
    monkeypatch.setattr(media.asyncio, "sleep", sleeps)
    source = SimpleNamespace(get_file=AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        await media.fetch_telegram_file(source, "attachment")
    transient = type(error) is NetworkError
    assert source.get_file.await_count == (3 if transient else 1)
    assert sleeps.await_count == (2 if transient else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
async def test_slow_retry_finishes_before_next_watchdog_probe(monkeypatch, recover):
    # Shorten time, not the contract: ingress must finish within one 90s probe interval.
    assert 0 < media._FETCH_BUDGET < 90
    monkeypatch.setattr(media, "_FETCH_BUDGET", 2.0)
    monkeypatch.setattr(media, "_FETCH_DELAYS", (0.0, 0.0))
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(**kwargs):
        reading.set()
        await release.wait()  # A slow-drip transfer may never hit httpx's inactivity timeout.
        return b"ogg"

    file_obj = _file_obj()
    file_obj.download_as_bytearray = read
    source = SimpleNamespace(file_size=3, get_file=AsyncMock(side_effect=[TimedOut("metadata"), file_obj]))
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(pending_update_count=1)))
    adapter._bot = bot
    adapter._app = SimpleNamespace(bot=bot, updater=SimpleNamespace(running=True))
    adapter._handle_polling_network_error = AsyncMock()
    adapter.handle_message = AsyncMock()
    msg = SimpleNamespace(reply_text=AsyncMock())
    event = _event()
    await adapter._probe_pending_updates(bot, 5)  # established baseline before the slow fetch

    async def consume():
        token = ingress_consumer_scope()
        try:
            await adapter._cache_inbound_av(msg, event, source, "voice message", "voice", ".ogg", "audio/ogg")
            await adapter.handle_message(event)
            # The next update on the same serial consumer can now be dispatched.
            await adapter._on_platform_update(SimpleNamespace(), SimpleNamespace())
        finally:
            leave_ingress_consumer(token)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(reading.wait(), 5)
        await adapter._probe_pending_updates(bot, 5)  # first no-progress observation, mid-retry
        assert adapter._polling_pending_stuck_count == 1
        assert not task.done()
        if recover:
            release.set()
        await asyncio.wait_for(task, 5)
        await asyncio.gather(*adapter._background_tasks)
        await adapter._probe_pending_updates(bot, 5)  # next 90s probe sees dispatch progress
        bot.get_webhook_info.return_value.pending_update_count = 0
        await adapter._probe_pending_updates(bot, 5)
        assert adapter._polling_pending_stuck_count == 0
        assert adapter._polling_error_task is None
        adapter._handle_polling_network_error.assert_not_called()
        adapter.handle_message.assert_awaited_once_with(event)
        assert bool(event.media_urls) is recover
        assert msg.reply_text.await_count == (0 if recover else 1)
        assert ("could not be downloaded" in event.text) is not recover
        assert source.get_file.await_count == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_exhaustion_sends_exactly_one_real_resend_notice(monkeypatch):
    monkeypatch.setattr(media, "_FETCH_DELAYS", (0.0, 0.0))
    methods, notices = [], []

    async def respond(request):
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "getFile":
            raise httpx.ReadTimeout("temporary read timeout", request=request)
        assert method == "sendMessage"
        notices.append(parse_qs(request.content.decode())["text"][0])
        return httpx.Response(200, json={"ok": True, "result": {
            "message_id": 2, "date": 0, "chat": {"id": 12345, "type": "private"}, "text": notices[-1]}})

    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(respond)})
    bot = Bot(token="123456:fake-test-token", request=request)
    msg = Message.de_json({"message_id": 1, "date": 0, "chat": {"id": 12345, "type": "private"},
                           "voice": {"file_id": "voice-id", "file_unique_id": "unique", "duration": 1, "file_size": 3}}, bot)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    event = _event()
    token = ingress_consumer_scope()
    try:
        await adapter._cache_inbound_av(msg, event, msg.voice, "voice message", "voice", ".ogg", "audio/ogg")
        await asyncio.gather(*adapter._background_tasks)
        assert methods == ["getFile"] * 3 + ["sendMessage"]
        assert len(notices) == 1 and "try sending it again" in notices[0].lower()
        assert "could not be downloaded" in event.text
        assert not event.media_urls
    finally:
        leave_ingress_consumer(token)
        await request.shutdown()
        await bot.shutdown()
