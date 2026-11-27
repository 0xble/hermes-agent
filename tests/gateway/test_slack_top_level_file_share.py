"""Regression coverage for Slack top-level DM file delivery."""

import asyncio
import importlib
from importlib.machinery import PathFinder
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig


class _SlackApiError(Exception):
    """Minimal Slack SDK error shape for import-time test doubles."""


def _load_installed_package(name):
    if PathFinder.find_spec(name) is None:
        return None
    prefix = f"{name}."
    displaced = {
        module_name: sys.modules.pop(module_name)
        for module_name in tuple(sys.modules)
        if (module_name == name or module_name.startswith(prefix))
        and not isinstance(sys.modules[module_name], ModuleType)
    }
    try:
        return importlib.import_module(name)
    except ImportError:
        sys.modules.update(displaced)
        return None


def _ensure_slack_mock():
    if _load_installed_package("slack_bolt") is None:
        slack_bolt = MagicMock()
        slack_bolt.async_app.AsyncApp = MagicMock
        slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock
        for name, module in (
            ("slack_bolt", slack_bolt),
            ("slack_bolt.async_app", slack_bolt.async_app),
            ("slack_bolt.adapter", slack_bolt.adapter),
            ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
            ("slack_bolt.adapter.socket_mode.async_handler", slack_bolt.adapter.socket_mode.async_handler),
        ):
            sys.modules.setdefault(name, module)
    if _load_installed_package("slack_sdk") is None:
        slack_sdk = MagicMock()
        slack_sdk.web.async_client.AsyncWebClient = MagicMock
        slack_sdk.errors.SlackApiError = _SlackApiError
        for name, module in (
            ("slack_sdk", slack_sdk),
            ("slack_sdk.errors", slack_sdk.errors),
            ("slack_sdk.web", slack_sdk.web),
            ("slack_sdk.web.async_client", slack_sdk.web.async_client),
            ("slack_sdk.web.async_slack_response", slack_sdk.web.async_slack_response),
        ):
            sys.modules.setdefault(name, module)
    sys.modules.setdefault("aiohttp", _load_installed_package("aiohttp") or MagicMock())


_ensure_slack_mock()
import plugins.platforms.slack.adapter as _slack_mod

_slack_mod.SLACK_AVAILABLE = True
from plugins.platforms.slack.adapter import SlackAdapter


TS = "1234567890.000001"


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    instance = SlackAdapter(PlatformConfig(enabled=True, token="***"))
    instance._app = MagicMock()
    instance._app.client = AsyncMock()
    instance._app.client.users_info = AsyncMock(
        return_value={
            "user": {
                "is_bot": False,
                "profile": {"display_name": "Test User"},
                "real_name": "Test User",
            }
        }
    )
    instance._bot_user_id = "U_BOT"
    instance._running = True
    instance.handle_message = AsyncMock()
    monkeypatch.setattr("gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache")
    monkeypatch.setattr("gateway.platforms.base.VIDEO_CACHE_DIR", tmp_path / "video_cache")
    return instance


def _file_info(*, stub=False):
    file_obj = {
        "id": "F_PDF",
        "name": "report.pdf",
        "mimetype": "application/pdf",
        "size": 24_368_299,
        "url_private_download": "https://files.slack.com/report.pdf",
        "shares": {"private": {"D123": [{"ts": TS}]}},
    }
    if stub:
        return {"ok": True, "file": {"id": "F_PDF", "name": "report.pdf", "file_access": "check_file_info"}}
    return {"ok": True, "file": file_obj}


def _file_shared_event():
    return {
        "file_id": "F_PDF",
        "channel_id": "D123",
        "user_id": "U_USER",
        "event_ts": TS,
    }


def _message(*, files=None, text="can you summarize this book"):
    return {
        "text": text,
        "user": "U_USER",
        "channel": "D123",
        "channel_type": "im",
        "ts": TS,
        **({"files": files} if files is not None else {}),
    }


def _message_file():
    return _file_info()["file"]


@pytest.mark.asyncio
@pytest.mark.parametrize("top_level_threads", [False, True])
async def test_text_and_pdf_message_dispatches_one_turn(adapter, top_level_threads):
    adapter.config.extra["dm_top_level_threads_as_sessions"] = top_level_threads
    with patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock) as download:
        download.return_value = b"%PDF-1.4 fake content"
        await adapter._handle_slack_message(_message(files=[_message_file()]))

    adapter.handle_message.assert_awaited_once()
    message = adapter.handle_message.await_args.args[0]
    assert "can you summarize this book" in message.text
    assert message.media_types == ["application/pdf"]
    assert message.media_urls
    assert message.message_id == TS
    assert message.source.thread_id == (TS if top_level_threads else None)
    download.assert_awaited_once_with(
        "https://files.slack.com/report.pdf",
        team_id="",
        max_bytes=_slack_mod._SLACK_INBOUND_DOCUMENT_MAX_BYTES,
    )


@pytest.mark.asyncio
async def test_metadata_over_inbound_limit_is_visible_without_download(adapter):
    oversized = {**_message_file(), "size": _slack_mod._SLACK_INBOUND_DOCUMENT_MAX_BYTES + 1}
    with patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock) as download:
        await adapter._handle_slack_message(_message(files=[oversized]))

    download.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    text = adapter.handle_message.await_args.args[0].text
    assert "[file: report.pdf (application/pdf)]" in text
    assert "exceeds the 100 MB inbound document limit" in text


class _StreamingResponse:
    status_code = 200
    headers = {"content-type": "application/octet-stream"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        yield b"1234"
        yield b"56789"


class _StreamingClient:
    def stream(self, *_args, **_kwargs):
        return _StreamingResponse()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


@pytest.mark.asyncio
async def test_unknown_size_stream_aborts_at_inbound_limit(adapter, monkeypatch):
    monkeypatch.setattr(_slack_mod, "_SLACK_INBOUND_DOCUMENT_MAX_BYTES", 8)
    with patch(
        "tools.url_safety.is_safe_url", return_value=True
    ), patch(
        "tools.url_safety.create_ssrf_safe_async_client", return_value=_StreamingClient()
    ), patch.object(adapter, "_is_slack_cdn_url", return_value=True):
        with pytest.raises(_slack_mod._SlackInboundFileTooLarge):
            await adapter._download_slack_file_bytes(
                "https://files.slack.com/report.pdf", max_bytes=8
            )


@pytest.mark.asyncio
async def test_unknown_size_over_limit_is_visible_in_same_turn(adapter):
    with patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock) as download:
        download.side_effect = _slack_mod._SlackInboundFileTooLarge(
            "Slack attachment exceeds the 100 MB inbound document limit."
        )
        await adapter._handle_slack_message(_message(files=[{**_message_file(), "size": 0}]))

    download.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()
    text = adapter.handle_message.await_args.args[0].text
    assert "[file: report.pdf (application/pdf)]" in text
    assert "exceeds the 100 MB inbound document limit" in text


@pytest.mark.asyncio
async def test_message_file_stub_is_resolved_via_files_info(adapter):
    adapter._app.client.files_info = AsyncMock(return_value=_file_info())
    with patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock) as download:
        download.return_value = b"%PDF-1.4 fake content"
        await adapter._handle_slack_message(_message(files=[_file_info(stub=True)["file"]]))

    adapter._app.client.files_info.assert_awaited_once_with(file="F_PDF")
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].media_types == ["application/pdf"]


@pytest.mark.asyncio
async def test_files_info_failure_is_visible_in_same_turn(adapter):
    adapter._app.client.files_info = AsyncMock(return_value={"ok": False, "error": "file_not_found"})
    await adapter._handle_slack_message(_message(files=[_file_info(stub=True)["file"]]))

    adapter.handle_message.assert_awaited_once()
    text = adapter.handle_message.await_args.args[0].text
    assert "[file: report.pdf]" in text
    assert "Slack attachment report.pdf is no longer available" in text


@pytest.mark.asyncio
async def test_download_failure_is_visible_in_same_turn(adapter):
    with patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock) as download:
        download.side_effect = RuntimeError("download failed")
        await adapter._handle_slack_message(_message(files=[_message_file()]))

    adapter.handle_message.assert_awaited_once()
    text = adapter.handle_message.await_args.args[0].text
    assert "[file: report.pdf (application/pdf)]" in text
    assert "Slack attachment report.pdf could not be downloaded" in text


@pytest.mark.asyncio
async def test_message_files_then_file_shared_deduplicates(adapter):
    adapter._app.client.files_info = AsyncMock(return_value=_file_info())
    with patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock) as download:
        download.return_value = b"%PDF-1.4 fake content"
        await adapter._handle_slack_message(_message(files=[_message_file()]))
        await adapter._handle_slack_file_shared(_file_shared_event())

    adapter.handle_message.assert_awaited_once()
    adapter._app.client.files_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_shared_then_message_files_deduplicates(adapter):
    adapter._app.client.files_info = AsyncMock(return_value=_file_info())
    with patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock) as download:
        download.return_value = b"%PDF-1.4 fake content"
        fallback = asyncio.create_task(adapter._handle_slack_file_shared(_file_shared_event()))
        await adapter._handle_slack_message(_message(files=[_message_file()]))
        await fallback

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_file_shared_uses_real_ts_and_skips_bot_upload(adapter):
    adapter.config.extra["dm_top_level_threads_as_sessions"] = True
    adapter._app.client.files_info = AsyncMock(return_value=_file_info())
    with patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock) as download:
        download.return_value = b"%PDF-1.4 fake content"
        await adapter._handle_slack_file_shared(_file_shared_event())

    message = adapter.handle_message.await_args.args[0]
    assert message.message_id == TS
    assert message.source.thread_id == TS
    assert adapter._resolve_thread_ts(None, {"thread_id": message.source.thread_id}) == TS

    adapter.handle_message.reset_mock()
    adapter._app.client.files_info.reset_mock()
    await adapter._handle_slack_file_shared({**_file_shared_event(), "user_id": "U_BOT"})
    adapter.handle_message.assert_not_awaited()
    adapter._app.client.files_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_shared_without_ts_dispatches_flat_with_empty_message_id(adapter):
    adapter._app.client.files_info = AsyncMock(
        return_value={
            "ok": True,
            "file": {
                **_message_file(),
                "shares": {"private": {"D123": [{"thread_ts": "THREAD_ONLY"}]}},
            },
        }
    )
    with patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock) as download:
        download.return_value = b"%PDF-1.4 fake content"
        await adapter._handle_slack_file_shared({
            "file_id": "F_PDF", "channel_id": "D123", "user_id": "U_USER"
        })

    message = adapter.handle_message.await_args.args[0]
    assert message.message_id == ""
    assert message.source.message_id is None
    assert message.source.thread_id is None
    assert message.reply_to_message_id is None
    assert "file:F_PDF" in adapter._processed_message_ts


@pytest.mark.asyncio
async def test_file_shared_info_failure_waits_grace_and_rechecks_file_claim(adapter):
    adapter._app.client.files_info = AsyncMock(return_value={"ok": False, "error": "not_found"})

    async def claim_file_during_grace(delay):
        assert delay == 0.75
        adapter._remember_processed_file_id("F_PDF")

    with patch.object(_slack_mod.asyncio, "sleep", new_callable=AsyncMock) as sleep:
        sleep.side_effect = claim_file_during_grace
        await adapter._handle_slack_file_shared(_file_shared_event())

    sleep.assert_awaited_once_with(0.75)
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolved_file_shared_without_ts_dispatches_flat_with_empty_message_id(adapter):
    adapter._app.client.files_info = AsyncMock(return_value={"ok": False, "error": "not_found"})
    await adapter._handle_slack_file_shared({
        "file_id": "F_PDF", "channel_id": "D123", "user_id": "U_USER",
        "file_name": "report.pdf", "mimetype": "application/pdf",
    })

    message = adapter.handle_message.await_args.args[0]
    assert message.message_id == ""
    assert message.source.message_id is None
    assert message.source.thread_id is None
    assert message.reply_to_message_id is None
    assert "[file: report.pdf (application/pdf)]" in message.text


@pytest.mark.asyncio
async def test_file_claim_is_released_when_message_handler_raises(adapter):
    adapter.handle_message.side_effect = RuntimeError("dispatch failed")
    with pytest.raises(RuntimeError, match="dispatch failed"):
        await adapter._handle_slack_message(_message(files=[_message_file()]))
    assert "file:F_PDF" not in adapter._processed_message_ts
