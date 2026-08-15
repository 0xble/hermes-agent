"""Provider-directed retry contract for idempotent media downloads."""

import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

import gateway.platforms.base as base
from tools import url_safety


_PNG = b"\x89PNG\r\n\x1a\n" + b"image"
_AUDIO = b"OggS" + b"audio"
_URL = "https://cdn.example.com/private/media.png?token=supersecret"


def _install_transport(monkeypatch, handler):
    calls = []
    sleeps = []

    def recording_handler(request):
        calls.append(request)
        return handler(request, len(calls))

    def make_client(**_kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(recording_handler))

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(url_safety, "create_ssrf_safe_async_client", make_client)
    monkeypatch.setattr(url_safety, "is_safe_url", lambda _url: True)
    monkeypatch.setattr(base.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(base.random, "uniform", lambda _low, high: high)
    return calls, sleeps


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("download", "cache_attr", "payload", "ext"),
    [
        (base.cache_image_from_url, "IMAGE_CACHE_DIR", _PNG, ".png"),
        (base.cache_audio_from_url, "AUDIO_CACHE_DIR", _AUDIO, ".ogg"),
    ],
)
async def test_image_and_audio_honor_retry_after_seconds(
    monkeypatch, tmp_path, download, cache_attr, payload, ext
):
    def handler(request, call_number):
        if call_number == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, request=request)
        return httpx.Response(200, content=payload, request=request)

    calls, sleeps = _install_transport(monkeypatch, handler)
    monkeypatch.setattr(base, cache_attr, tmp_path)

    cached = await download(_URL, ext=ext, retries=1)

    assert len(calls) == 2
    assert sleeps == [7.0]
    assert (tmp_path / cached.rsplit("/", 1)[-1]).read_bytes() == payload


@pytest.mark.asyncio
async def test_retry_after_http_date_is_a_minimum_delay(monkeypatch):
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=12)

    def handler(request, call_number):
        if call_number == 1:
            return httpx.Response(
                503,
                headers={"Retry-After": format_datetime(retry_at, usegmt=True)},
                request=request,
            )
        return httpx.Response(200, content=_PNG, request=request)

    calls, sleeps = _install_transport(monkeypatch, handler)

    content = await base._download_media_from_url(
        _URL, media_type="image", accept="image/*", retries=1
    )

    assert content == _PNG
    assert len(calls) == 2
    assert len(sleeps) == 1
    assert 10.0 <= sleeps[0] <= 12.0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 502, 503, 504])
async def test_only_selected_transient_statuses_retry(monkeypatch, status):
    def handler(request, call_number):
        return httpx.Response(
            status if call_number == 1 else 200,
            content=_PNG if call_number > 1 else b"",
            request=request,
        )

    calls, sleeps = _install_transport(monkeypatch, handler)

    content = await base._download_media_from_url(
        _URL, media_type="image", accept="image/*", retries=1
    )

    assert content == _PNG
    assert len(calls) == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 408, 500, 501, 505])
async def test_permanent_http_statuses_fail_closed_without_retry(
    monkeypatch, status
):
    def handler(request, _call_number):
        return httpx.Response(status, request=request)

    calls, sleeps = _install_transport(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError):
        await base._download_media_from_url(
            _URL, media_type="image", accept="image/*", retries=2
        )

    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_connection_establishment_failures_retry(monkeypatch, error_type):
    def handler(request, call_number):
        if call_number == 1:
            raise error_type("connection unavailable", request=request)
        return httpx.Response(200, content=_PNG, request=request)

    calls, sleeps = _install_transport(monkeypatch, handler)

    content = await base._download_media_from_url(
        _URL, media_type="image", accept="image/*", retries=1
    )

    assert content == _PNG
    assert len(calls) == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_post_connect_read_timeout_does_not_retry(monkeypatch):
    def handler(request, _call_number):
        raise httpx.ReadTimeout("read stalled", request=request)

    calls, sleeps = _install_transport(monkeypatch, handler)

    with pytest.raises(httpx.ReadTimeout):
        await base._download_media_from_url(
            _URL, media_type="image", accept="image/*", retries=2
        )

    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_provider_delay_beyond_budget_fails_without_sleep_and_sanitizes_log(
    monkeypatch, caplog
):
    def handler(request, _call_number):
        return httpx.Response(503, headers={"Retry-After": "31"}, request=request)

    calls, sleeps = _install_transport(monkeypatch, handler)

    with caplog.at_level(logging.WARNING, logger=base.__name__):
        with pytest.raises(httpx.HTTPStatusError):
            await base._download_media_from_url(
                _URL, media_type="image", accept="image/*", retries=2
            )

    assert len(calls) == 1
    assert sleeps == []
    assert "https://cdn.example.com/.../media.png" in caplog.text
    assert "supersecret" not in caplog.text


@pytest.mark.asyncio
async def test_cumulative_provider_delays_cannot_exceed_wait_budget(monkeypatch):
    def handler(request, _call_number):
        return httpx.Response(503, headers={"Retry-After": "20"}, request=request)

    calls, sleeps = _install_transport(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError):
        await base._download_media_from_url(
            _URL, media_type="image", accept="image/*", retries=2
        )

    assert len(calls) == 2
    assert sleeps == [20.0]
