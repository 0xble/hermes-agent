"""A new Telegram polling generation proves health with a non-blocking first getUpdates.

Planned restarts logged ``Telegram polling confirmed healthy`` ~11s after connect because the
generation's first getUpdates was an idle 10s long poll. Only the first request of each current
generation may skip the long-poll wait; steady-state polls and stale generations keep it.
"""

import asyncio

import pytest

pytest.importorskip("telegram.request", reason="python-telegram-bot not installed")
from telegram.request import BaseRequest, RequestData
from telegram.request._requestparameter import RequestParameter

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as tg_adapter
from plugins.platforms.telegram.adapter import TelegramAdapter

_BASE_READ_TIMEOUT = 7.0
_LONG_POLL = 10


class _GeneralRequest(BaseRequest):
    @property
    def read_timeout(self):
        return _BASE_READ_TIMEOUT

    async def initialize(self):
        return None

    async def shutdown(self):
        return None

    async def do_request(self, url, method, request_data=None, **_kwargs):
        if url.endswith("/getMe"):
            return 200, (
                b'{"ok":true,"result":{"id":1,"is_bot":true,'
                b'"first_name":"Test","username":"test_bot"}}')
        return 200, b'{"ok":true,"result":true}'


class _RecordingPollRequest(BaseRequest):
    """Idle Telegram: a long poll blocks for its full timeout; timeout=0 answers at once."""

    def __init__(self):
        self.calls = []
        self.pending = []
        self.release = asyncio.Event()
        self.long_poll_started = asyncio.Event()

    @property
    def read_timeout(self):
        return _BASE_READ_TIMEOUT

    async def initialize(self):
        return None

    async def shutdown(self):
        return None

    async def do_request(self, url, method, request_data=None, read_timeout=None, **_kwargs):
        params = request_data.parameters if request_data is not None else {}
        self.calls.append({
            "timeout": params.get("timeout"), "offset": params.get("offset"), "read_timeout": read_timeout,
            "generation": tg_adapter._POLLING_GENERATION_CONTEXT.get()})
        if params.get("timeout"):
            self.long_poll_started.set()
            await self.release.wait()
        batch, self.pending = self.pending, []
        body = ",".join(
            '{"update_id":%d,"message":{"message_id":%d,"date":0,'
            '"chat":{"id":1,"type":"private"},"text":"hi"}}' % (uid, uid) for uid in batch)
        return 200, ('{"ok":true,"result":[%s]}' % body).encode()


def _polls(request, generation):
    return [c for c in request.calls if c["generation"] == generation]


@pytest.mark.asyncio
async def test_each_generation_proves_health_with_one_non_blocking_first_poll():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="123456:test-token"))
    polling_request = _RecordingPollRequest()
    polling_request.pending = [41]  # a backlog update must come through the fast poll exactly once
    app = (
        tg_adapter.Application.builder().token("123456:test-token").request(_GeneralRequest())
        .get_updates_request(adapter._instrument_polling_request(polling_request)).build())
    adapter._app = app
    await app.initialize()
    try:
        # Cold boot generation.
        generation, progress = await adapter._start_polling_once(
            app, drop_pending_updates=False, error_callback=lambda _e: None, schedule_verifier=False)
        await asyncio.wait_for(progress.wait(), timeout=1)
        await asyncio.wait_for(polling_request.long_poll_started.wait(), timeout=1)
        first, second = _polls(polling_request, generation)[:2]
        assert first["timeout"] == 0
        assert first["read_timeout"] == _BASE_READ_TIMEOUT
        # Steady state keeps PTB's long poll and its widened read timeout; the update was acknowledged.
        assert second["timeout"] == _LONG_POLL
        assert second["read_timeout"] == _BASE_READ_TIMEOUT + _LONG_POLL
        assert second["offset"] == 42
        assert app.update_queue.qsize() == 1 and (await app.update_queue.get()).update_id == 41

        # Reconnect generation: stop (PTB's own timeout=0 cleanup poll), start again.
        polling_request.release.set()
        await app.updater.stop()
        polling_request.release = asyncio.Event()
        polling_request.long_poll_started = asyncio.Event()
        reconnect, reconnect_progress = await adapter._start_polling_once(
            app, drop_pending_updates=False, error_callback=lambda _e: None, schedule_verifier=False)
        assert reconnect == generation + 1
        await asyncio.wait_for(reconnect_progress.wait(), timeout=1)
        await asyncio.wait_for(polling_request.long_poll_started.wait(), timeout=1)
        r_first, r_second = _polls(polling_request, reconnect)[:2]
        assert r_first["timeout"] == 0
        assert r_second["timeout"] == _LONG_POLL
        assert r_second["offset"] == 42
    finally:
        polling_request.release.set()
        if app.updater.running:
            await app.updater.stop()
        await app.shutdown()


@pytest.mark.asyncio
async def test_stale_or_unknown_generation_polls_keep_the_long_poll():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="123456:test-token"))
    current, _progress = adapter._begin_polling_generation()
    request = _RecordingPollRequest()
    request.release.set()
    instrumented = adapter._instrument_polling_request(request)
    data = RequestData([RequestParameter.from_input("timeout", _LONG_POLL), RequestParameter.from_input("offset", 3)])
    for generation in (current - 1, None):
        token = tg_adapter._POLLING_GENERATION_CONTEXT.set(generation)
        try:
            await instrumented.do_request(
                url="https://api.telegram.org/botX/getUpdates", method="POST", request_data=data,
                read_timeout=_BASE_READ_TIMEOUT + _LONG_POLL)
        finally:
            tg_adapter._POLLING_GENERATION_CONTEXT.reset(token)
    assert [c["timeout"] for c in request.calls] == [_LONG_POLL, _LONG_POLL]
    assert [c["read_timeout"] for c in request.calls] == [_BASE_READ_TIMEOUT + _LONG_POLL] * 2
