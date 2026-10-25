"""Slack-specific send_message delivery regressions.

Salvaged from #47547 and adapted to the post-#41112 plugin layout: the legacy
``_send_slack`` helper moved to ``plugins/platforms/slack/adapter.py::
_standalone_send`` and text sends now route through ``_send_via_adapter``
(live adapter first, registry standalone fallback).
"""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

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


def test_standalone_disabled_rich_mode_stays_text(monkeypatch, _standalone_send):
    fake = _SlackSession()
    monkeypatch.setattr("aiohttp.ClientSession", lambda *args, **kwargs: fake)
    pconfig = SimpleNamespace(enabled=True, token="good-token", extra={})
    result = asyncio.run(_standalone_send(pconfig, "D1", "![Photo](https://example.com/p.png)"))
    assert result["success"]
    assert "blocks" not in fake.calls[0][1]
