import asyncio
import threading
from pathlib import Path

import pytest

from gateway.platforms import base


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("async_name", "sync_name", "args"),
    [
        ("cache_image_from_bytes_async", "cache_image_from_bytes", (b"data", ".png")),
        ("cache_audio_from_bytes_async", "cache_audio_from_bytes", (b"data", ".ogg")),
        ("cache_video_from_bytes_async", "cache_video_from_bytes", (b"data", ".mp4")),
        (
            "cache_document_from_bytes_async",
            "cache_document_from_bytes",
            (b"data", "report.pdf"),
        ),
    ],
)
async def test_async_cache_wrappers_keep_event_loop_responsive(
    monkeypatch, async_name, sync_name, args
):
    loop_thread = threading.get_ident()
    cache_started = threading.Event()
    release_cache = threading.Event()
    observed = {}

    def blocking_cache(*call_args):
        observed["thread"] = threading.get_ident()
        observed["args"] = call_args
        cache_started.set()
        observed["ticker_ran_during_cache"] = release_cache.wait(timeout=1)
        return "cached"

    monkeypatch.setattr(base, sync_name, blocking_cache)

    async def ticker():
        while not cache_started.is_set():
            await asyncio.sleep(0)
        release_cache.set()

    ticker_task = asyncio.create_task(ticker())
    result = await getattr(base, async_name)(*args)
    await ticker_task

    assert result == "cached"
    assert observed["args"] == args
    assert observed["thread"] != loop_thread
    assert observed["ticker_ran_during_cache"] is True


@pytest.mark.asyncio
async def test_async_cache_wrapper_propagates_validation_errors(monkeypatch):
    def reject_image(data, ext):
        raise ValueError("invalid image")

    monkeypatch.setattr(base, "cache_image_from_bytes", reject_image)

    with pytest.raises(ValueError, match="invalid image"):
        await base.cache_image_from_bytes_async(b"not-an-image", ".png")


@pytest.mark.asyncio
async def test_cache_media_bytes_async_runs_off_loop_and_forwards_kwargs(monkeypatch):
    loop_thread = threading.get_ident()
    observed = {}

    def fake_cache_media_bytes(data, *, filename="", mime_type="", default_kind=None):
        observed["thread"] = threading.get_ident()
        observed["call"] = (data, filename, mime_type, default_kind)
        return "cached-media"

    monkeypatch.setattr(base, "cache_media_bytes", fake_cache_media_bytes)

    result = await base.cache_media_bytes_async(
        b"payload", filename="report.pdf", mime_type="application/pdf", default_kind="document"
    )

    assert result == "cached-media"
    assert observed["call"] == (b"payload", "report.pdf", "application/pdf", "document")
    assert observed["thread"] != loop_thread


@pytest.mark.asyncio
async def test_async_cache_wrapper_uses_active_profile_home(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    cached = await base.cache_image_from_bytes_async(
        b"\x89PNG\r\n\x1a\nminimal", ".png"
    )

    cached_path = Path(cached)
    assert cached_path.parent == profile_home / "cache" / "images"
    assert cached_path.read_bytes() == b"\x89PNG\r\n\x1a\nminimal"


def test_heic_screenshot_is_cached_as_png(tmp_path, monkeypatch):
    """2026-10-24: a Slack HEIC screenshot was refused as "non-image data", so the turn ran blind."""
    pytest.importorskip("pillow_heif")
    from io import BytesIO

    import pillow_heif
    from PIL import Image

    monkeypatch.setattr(base, "get_image_cache_dir", lambda: tmp_path)
    buf = BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", (4, 4), "red")).save(buf, format="HEIF")
    heic = buf.getvalue()
    assert heic[4:8] == b"ftyp" and not base._looks_like_image(heic)

    path = Path(base.cache_image_from_bytes(heic, ".heic"))

    assert path.suffix == ".png"
    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_html_error_page_is_still_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "get_image_cache_dir", lambda: tmp_path)
    with pytest.raises(ValueError, match="non-image data"):
        base.cache_image_from_bytes(b"<!DOCTYPE html><html>sign in</html>", ".jpg")
    with pytest.raises(ValueError, match="non-image data"):
        base.cache_image_from_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', ".png")
