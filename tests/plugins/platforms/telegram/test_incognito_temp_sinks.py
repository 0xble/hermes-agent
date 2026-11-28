"""Telegram adapter temp sinks: a private turn stages copies only inside its interval."""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from agent import incognito_private_dir as private_dirs
from agent.incognito import incognito_scope, register_session
from gateway.config import Platform
from gateway.platforms.base import SendResult
from plugins.platforms.telegram import telegram_audio_meta
from plugins.platforms.telegram.adapter import TelegramAdapter

PRIVATE, UNBOUND, PUBLIC = "tg-sink-private", "tg-sink-unbound", "tg-sink-public"


@pytest.fixture
def interval():
    register_session(PRIVATE)
    with incognito_scope(True, PRIVATE):
        root = private_dirs.private_path_for_current_session()
    try:
        yield root
    finally:
        register_session(PRIVATE, False)
        private_dirs.discard_private_dir(PRIVATE)


@pytest.fixture
def public_tmp(tmp_path, monkeypatch):
    path = tmp_path / "public-temp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


def _under(path, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root.resolve())


def _files(path: Path) -> list[Path]:
    return [p for p in path.rglob("*") if p.is_file()]


# --- Oversize copy-block document -------------------------------------------------------

@pytest.fixture
def copy_adapter():
    adapter = object.__new__(TelegramAdapter)
    seen = {}
    adapter.platform = Platform.TELEGRAM
    adapter._text_send_refusal = lambda: None
    adapter._send_flood_cooldown_remaining = lambda chat_id: None
    adapter._chat_outbound_slot_remaining = lambda chat_id: 0
    adapter._hold_chat_outbound_slot = lambda chat_id: None
    adapter._telegram_error_types = lambda: ()

    async def send_local_file(label, path, *_args, **_kwargs):
        seen["path"] = path
        seen["body"] = Path(path).read_text(encoding="utf-8")
        return SendResult(success=True, message_id="1")

    adapter._send_local_file = send_local_file
    return adapter, seen


def _send_copy(adapter):
    body = "x" * (TelegramAdapter.MAX_MESSAGE_LENGTH + 10)
    return asyncio.run(adapter._send_text_locked("c1", body, None, {"copy_block": True}))


def test_copy_block_document_in_interval(interval, copy_adapter, public_tmp):
    adapter, seen = copy_adapter
    with incognito_scope(True, PRIVATE):
        assert _send_copy(adapter).success
    assert _under(seen["path"], interval) and len(seen["body"]) > TelegramAdapter.MAX_MESSAGE_LENGTH
    assert _files(public_tmp) == []
    assert not os.path.exists(seen["path"])  # removed after upload


def test_copy_block_document_fails_closed_without_interval(copy_adapter, public_tmp):
    adapter, seen = copy_adapter
    with incognito_scope(True, UNBOUND):
        result = _send_copy(adapter)
    assert not result.success and "path" not in seen and _files(public_tmp) == []


def test_copy_block_document_public_control(interval, copy_adapter, public_tmp):
    adapter, seen = copy_adapter
    with incognito_scope(False, PUBLIC):
        assert _send_copy(adapter).success
    assert _under(seen["path"], public_tmp)


# --- Large-image JPEG copy -----------------------------------------------------------------

@pytest.fixture
def big_png(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    path = tmp_path / "generated.png"
    # Random noise does not compress, so the file exceeds the 1 MB compression threshold.
    PIL.frombytes("RGB", (900, 900), os.urandom(900 * 900 * 3)).save(path, "PNG")
    assert path.stat().st_size > TelegramAdapter._IMG_COMPRESS_THRESHOLD_BYTES
    return str(path)


def _compress(path):
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM  # ``name`` (used in log lines) derives from it
    return adapter._compress_image_to_jpeg(path)


def test_image_compression_in_interval(interval, big_png, public_tmp):
    with incognito_scope(True, PRIVATE):
        compressed = _compress(big_png)
    assert compressed and _under(compressed, interval) and _files(public_tmp) == []


def test_image_compression_fails_closed_without_interval(big_png, public_tmp):
    with incognito_scope(True, UNBOUND):
        assert _compress(big_png) is None  # the original is uploaded instead
    assert _files(public_tmp) == []


def test_image_compression_public_control(interval, big_png, public_tmp):
    with incognito_scope(False, PUBLIC):
        compressed = _compress(big_png)
    assert compressed and _under(compressed, public_tmp)


# --- Audio cover / video thumbnail -------------------------------------------------------------

@pytest.fixture
def fake_thumbnail(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    def run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(telegram_audio_meta.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(telegram_audio_meta.subprocess, "run", run)
    assert subprocess  # keep the real module importable for the patched attribute


def test_thumbnail_in_interval(interval, fake_thumbnail, public_tmp):
    with incognito_scope(True, PRIVATE):
        thumb = telegram_audio_meta.fit_thumbnail_jpeg("clip.mp4", seek=1)
        cover = telegram_audio_meta._extract_cover_jpeg("song.mp3")
    assert thumb and cover and _under(thumb, interval) and _under(cover, interval)
    assert _files(public_tmp) == []


def test_thumbnail_fails_closed_without_interval(fake_thumbnail, public_tmp):
    with incognito_scope(True, UNBOUND):
        assert telegram_audio_meta.fit_thumbnail_jpeg("clip.mp4", seek=1) is None
        assert telegram_audio_meta._extract_cover_jpeg("song.mp3") is None
    assert _files(public_tmp) == []


def test_thumbnail_public_control(interval, fake_thumbnail, public_tmp):
    with incognito_scope(False, PUBLIC):
        thumb = telegram_audio_meta.fit_thumbnail_jpeg("clip.mp4", seek=1)
    assert thumb and _under(thumb, public_tmp)
