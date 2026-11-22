"""Regression coverage for Slack top-level DM file_shared delivery."""

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


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    config = PlatformConfig(enabled=True, token="***")
    instance = SlackAdapter(config)
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


def _file_info():
    return {
        "ok": True,
        "file": {
            "id": "F_PDF",
            "name": "report.pdf",
            "mimetype": "application/pdf",
            "size": 21,
            "url_private_download": "https://files.slack.com/report.pdf",
            "shares": {"private": {"D123": [{"ts": "1234567890.000001"}]}},
        },
    }


def _file_shared_event():
    return {
        "file_id": "F_PDF",
        "channel_id": "D123",
        "user_id": "U_USER",
        "event_ts": "1234567890.000001",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("top_level_threads", [False, True])
async def test_top_level_file_shared_pdf_reaches_session(adapter, top_level_threads):
    adapter.config.extra["dm_top_level_threads_as_sessions"] = top_level_threads
    adapter._app.client.files_info = AsyncMock(return_value=_file_info())
    with (
        patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock) as download,
        patch.object(_slack_mod.asyncio, "sleep", new_callable=AsyncMock),
    ):
        download.return_value = b"%PDF-1.4 fake content"
        await adapter._handle_slack_file_shared(_file_shared_event())

    adapter.handle_message.assert_awaited_once()
    message = adapter.handle_message.await_args.args[0]
    assert message.media_types == ["application/pdf"]
    assert message.media_urls
    assert message.source.thread_id == ("1234567890.000001" if top_level_threads else None)


@pytest.mark.asyncio
async def test_file_shared_pdf_is_not_lost_after_text_message(adapter):
    """A text-only event cannot suppress a later file_shared failure notice."""
    adapter.config.extra["dm_top_level_threads_as_sessions"] = False
    await adapter._handle_slack_message({
        "text": "can you summarize this book",
        "user": "U_USER",
        "channel": "D123",
        "channel_type": "im",
        "ts": "1234567890.000001",
    })
    adapter._app.client.files_info = AsyncMock(return_value=_file_info())
    with (
        patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock) as download,
        patch.object(_slack_mod.asyncio, "sleep", new_callable=AsyncMock),
    ):
        download.side_effect = RuntimeError("download failed")
        await adapter._handle_slack_file_shared(_file_shared_event())

    assert adapter.handle_message.await_count == 2
    followup = adapter.handle_message.await_args_list[1].args[0]
    assert followup.media_types == []
    assert followup.media_urls == []
    assert "[file: report.pdf (application/pdf)]" in followup.text
    assert "Slack attachment notice" in followup.text
