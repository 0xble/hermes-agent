"""Slack-specific send_message delivery regressions.

Salvaged from #47547 and adapted to the post-#41112 plugin layout: the legacy
``_send_slack`` helper moved to ``plugins/platforms/slack/adapter.py::
_standalone_send`` and text sends now route through ``_send_via_adapter``
(live adapter first, registry standalone fallback).
"""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest



def _ensure_slack_mock(monkeypatch):
    """Install lightweight Slack modules when optional Slack deps are absent."""
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return

    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock

    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock

    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler", slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)




class _SlackResponse:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _SlackPostContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SlackSession:
    """Fake aiohttp session whose good-token posts succeed."""

    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, *, headers, json, **kwargs):
        token = headers["Authorization"].removeprefix("Bearer ")
        self.calls.append((token, json))
        if token == "good-token":
            payload = {"ok": True, "ts": "171.123"}
        else:
            payload = {"ok": False, "error": "invalid_auth"}
        return _SlackPostContext(_SlackResponse(payload))


@pytest.fixture
def _standalone_send(monkeypatch):
    _ensure_slack_mock(monkeypatch)
    from plugins.platforms.slack import adapter as slack_adapter

    return slack_adapter._standalone_send


def test_standalone_send_stops_on_non_token_error(monkeypatch, _standalone_send):
    """Terminal errors (not token-scoped) must not burn the remaining tokens."""

    class _FatalSession(_SlackSession):
        def post(self, url, *, headers, json, **kwargs):
            token = headers["Authorization"].removeprefix("Bearer ")
            self.calls.append((token, json))
            return _SlackPostContext(
                _SlackResponse({"ok": False, "error": "msg_too_long"})
            )

    fake_session = _FatalSession()
    monkeypatch.setattr(
        "aiohttp.ClientSession", lambda *args, **kwargs: fake_session
    )

    pconfig = SimpleNamespace(enabled=True, token="tok-a,tok-b", extra={})
    result = asyncio.run(_standalone_send(pconfig, "C123", "hello"))

    assert result == {"error": "Slack API error: msg_too_long"}
    assert len(fake_session.calls) == 1


@pytest.mark.parametrize("reject", [False, True])
def test_standalone_rich_delivery_and_text_retry(monkeypatch, _standalone_send, reject):
    import copy

    class RichSession(_SlackSession):
        def post(self, url, *, headers, json, **kwargs):
            self.calls.append(copy.deepcopy(json))
            error = reject and "blocks" in json
            response = {"ok": False, "error": "invalid_blocks"} if error else {"ok": True, "ts": "171.123"}
            return _SlackPostContext(_SlackResponse(response))

    fake = RichSession()
    monkeypatch.setattr("aiohttp.ClientSession", lambda *args, **kwargs: fake)
    pconfig = SimpleNamespace(enabled=True, token="good-token", extra={"rich_blocks": True})
    text = "![Photo](https://example.com/photo.png)\n\n-# Source"
    result = asyncio.run(_standalone_send(pconfig, "D1", text, thread_id="123.456"))
    assert result["success"]
    assert [b["type"] for b in fake.calls[0]["blocks"]] == ["image", "context"]
    assert fake.calls[0]["text"] == text
    assert fake.calls[0]["thread_ts"] == "123.456"
    assert len(fake.calls) == (2 if reject else 1)
    if reject:
        assert "blocks" not in fake.calls[1]
        assert fake.calls[1]["text"] == fake.calls[0]["text"]
        assert fake.calls[1]["channel"] == "D1"
        assert fake.calls[1]["thread_ts"] == "123.456"


@pytest.mark.parametrize("route", ["standalone", "live"])
@pytest.mark.parametrize("rich,reject", [(True, False), (True, True), (False, False)])
def test_media_caption_blocks_and_fallback_match_both_routes(
    monkeypatch, _standalone_send, tmp_path, route, rich, reject
):
    from gateway.config import PlatformConfig
    from plugins.platforms.slack import adapter as slack_adapter
    from tools.send_message_tool import _send_live_adapter_media

    client = AsyncMock()
    client.chat_postMessage.side_effect = (
        [{"ok": False, "error": "invalid_blocks"}, {"ok": True, "ts": "171.123"}]
        if reject else [{"ok": True, "ts": "171.123"}]
    )
    client.files_upload_v2.return_value = {"ok": True, "file": {"timestamp": "171.124"}}
    monkeypatch.setattr("slack_sdk.web.async_client.AsyncWebClient", lambda **kwargs: client)
    config = PlatformConfig(enabled=True, token="good-token", extra={"rich_blocks": rich})
    document = tmp_path / "report.pdf"
    document.write_bytes(b"document")
    text = (
        "![Photo](https://example.com/p.png)\n\n-# Source\n\n"
        "```cards\n- title: Details\n  url: https://example.com/details\n```"
    )
    media = [(str(document), False)]
    if route == "standalone":
        result = asyncio.run(_standalone_send(
            config, "D1", text, caption=text, media_files=media, thread_id="123.456"))
    else:
        adapter = slack_adapter.SlackAdapter(config)
        adapter._app = MagicMock()
        adapter._get_client = MagicMock(return_value=client)
        result = asyncio.run(_send_live_adapter_media(
            adapter, "D1", text, media, thread_id="123.456", metadata={"thread_id": "123.456"}))
    assert result["success"]
    upload = client.files_upload_v2.await_args.kwargs
    assert upload["channel"] == "D1"
    assert upload["thread_ts"] == "123.456"
    assert upload["file"] == str(document)
    if not rich:
        assert client.chat_postMessage.await_count == 0
        expected = slack_adapter._standalone_format_mrkdwn(text) if route == "standalone" else text
        assert upload["initial_comment"] == expected
        return
    assert client.chat_postMessage.await_count == (2 if reject else 1)
    posted = client.chat_postMessage.await_args_list[0].kwargs
    assert [b["type"] for b in posted["blocks"]] == ["image", "context", "carousel"]
    assert all("actions" not in card for card in posted["blocks"][2]["elements"])
    assert posted["text"] == slack_adapter._standalone_format_mrkdwn(text)
    assert posted["channel"] == "D1"
    assert posted["thread_ts"] == "123.456"
    assert upload["initial_comment"] == ""
    if reject:
        fallback = client.chat_postMessage.await_args_list[1].kwargs
        assert fallback == {key: value for key, value in posted.items() if key != "blocks"}


def test_standalone_disabled_rich_mode_stays_text(monkeypatch, _standalone_send):
    fake = _SlackSession()
    monkeypatch.setattr("aiohttp.ClientSession", lambda *args, **kwargs: fake)
    pconfig = SimpleNamespace(enabled=True, token="good-token", extra={})
    result = asyncio.run(_standalone_send(pconfig, "D1", "![Photo](https://example.com/p.png)"))
    assert result["success"]
    assert "blocks" not in fake.calls[0][1]
