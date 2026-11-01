"""Read-only fetch recovery uses a fresh, policy-preserving Bot API client, not a user account."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

pytest.importorskip("telegram", reason="integration boundary requires the optional telegram extra")

from telegram import Bot
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut
from telegram.request import HTTPXRequest

from plugins.platforms.telegram import telegram_media_download as media


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["metadata", "bytes"])
@pytest.mark.parametrize("read_timeout", [20.0, 90.0])
async def test_exhausted_shared_pool_recovers_without_restarting_live_bot(monkeypatch, failure_stage, read_timeout):
    monkeypatch.setenv("HERMES_TELEGRAM_HTTP_READ_TIMEOUT", str(read_timeout))
    sleeps = AsyncMock()
    monkeypatch.setattr(media.asyncio, "sleep", sleeps)
    calls = []
    requests = []
    token = "123456:fake-test-token"

    async def respond(request):
        calls.append((request.method, request.url.path))
        assert request.extensions["timeout"]["read"] == read_timeout * 3
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {
                "file_id": "voice-id", "file_unique_id": "unique", "file_size": 3,
                "file_path": "voice/test.oga"}})
        assert request.url.path == f"/files/{token}/voice/test.oga"
        return httpx.Response(200, content=b"ogg")

    async def factory():
        pair = tuple(HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(respond)}) for _ in range(2))
        requests.extend(pair)
        return pair

    original = Bot(token=token, base_url="https://custom.example/bot", base_file_url="https://custom.example/files/")
    file_obj = SimpleNamespace(download_as_bytearray=AsyncMock(side_effect=TimedOut("read timeout")))
    get_file = AsyncMock(side_effect=TimedOut("metadata timeout")) if failure_stage == "metadata" else AsyncMock(return_value=file_obj)
    source = SimpleNamespace(get_bot=lambda: original, file_id="voice-id", get_file=get_file)
    try:
        fetched, data = await media.fetch_telegram_file(source, "voice message", request_factory=factory)
        assert data == b"ogg" and fetched.file_id == "voice-id"
        assert [call.kwargs["read_timeout"] for call in get_file.await_args_list] == [read_timeout * attempt for attempt in (1, 2, 3)]
        assert [call.args[0] for call in sleeps.await_args_list] == [2.0, 5.0]
        assert calls == [("POST", f"/bot{token}/getFile"), ("GET", f"/files/{token}/voice/test.oga")]
        # The isolated pools are closed, and no initialize/getMe/logout/polling call touched the live bot.
        assert all(request._client.is_closed for request in requests)
    finally:
        await original.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [BadRequest("unavailable"), Forbidden("blocked"), asyncio.CancelledError(), NetworkError("offline")])
async def test_permanent_errors_and_cancellation_never_enter_fallback(monkeypatch, error):
    sleeps = AsyncMock()
    fallback = AsyncMock(side_effect=TimedOut("fallback offline"))
    monkeypatch.setattr(media.asyncio, "sleep", sleeps)
    monkeypatch.setattr(media, "_fresh_request_fetch", fallback)
    source = SimpleNamespace(get_file=AsyncMock(side_effect=error))
    factory = AsyncMock()
    expected = TimedOut if type(error) is NetworkError else type(error)
    with pytest.raises(expected):
        await media.fetch_telegram_file(source, "attachment", request_factory=factory)
    transient = type(error) is NetworkError
    assert source.get_file.await_count == (3 if transient else 1)
    assert fallback.await_count == (1 if transient else 0)
    assert sleeps.await_count == (2 if transient else 0)
