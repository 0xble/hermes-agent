"""Proxy queue ownership transfers only on an explicit successful handling result."""
import asyncio
import json
import socket
from unittest.mock import patch

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.shutdown_flush import checkpoint_queued_event
from tests.gateway.test_proxy_mode import (
    _FakeSSEResponse, _FakeSession, _make_runner, _make_source, _patch_aiohttp,
)


def _queued_turn(tmp_path, monkeypatch, url):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("GATEWAY_PROXY_KEY", raising=False)
    runner = _make_runner()
    monkeypatch.setattr(runner, "_get_proxy_url", lambda: url)
    event = MessageEvent(text="accepted instruction", source=_make_source(),
                         message_id="proxy-queued", message_type=MessageType.TEXT)
    assert checkpoint_queued_event("proxy-key", event)
    event._test_checkpoint_path = event._hermes_recovery_spool
    return runner, event


async def _run(runner, event):
    with patch("gateway.run._load_gateway_config", return_value={}):
        return await runner._run_agent_inner(
            message=event.text, context_prompt="", history=[], source=event.source,
            session_id="proxy-session", session_key="proxy-key", queued_event=event,
        )


def _assert_retained(event, result):
    assert result["api_calls"] == 0 and result["messages"] == []
    assert event._test_checkpoint_path.exists()
    payload = json.loads(event._test_checkpoint_path.read_text())
    assert payload["data"]["text"] == event.text
    assert payload["data"]["message_id"] == event.message_id


@pytest.mark.asyncio
async def test_refused_loopback_retains_accepted_queue_checkpoint(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp")
    # Keep an ephemeral port reserved but not listening: deterministically refused.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{reserved.getsockname()[1]}"
        runner, event = _queued_turn(tmp_path, monkeypatch, url)
        result = await _run(runner, event)
    _assert_retained(event, result)
    assert "Proxy connection error" in result["final_response"]


@pytest.mark.asyncio
async def test_non200_loopback_retains_accepted_queue_checkpoint(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp")

    async def unavailable(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 11\r\n"
                         b"Connection: close\r\n\r\nunavailable")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(unavailable, "127.0.0.1", 0)
    async with server:
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        runner, event = _queued_turn(tmp_path, monkeypatch, url)
        result = await _run(runner, event)
    _assert_retained(event, result)
    assert "503" in result["final_response"]


class _FailedStream(_FakeSSEResponse):
    async def iter_any(self):
        raise ConnectionResetError("stream failed before admission")
        yield  # keep the response an async iterator


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    _FakeSSEResponse(),
    _FakeSSEResponse(sse_chunks=['data: {"error":"not admitted"}\n\n']),
    _FailedStream(),
], ids=["empty-eof", "error-frame-eof", "stream-failure"])
async def test_pre_admission_stream_failure_retains_queue_checkpoint(tmp_path, monkeypatch, response):
    runner, event = _queued_turn(tmp_path, monkeypatch, "http://proxy.invalid")
    with _patch_aiohttp(_FakeSession(response)):
        result = await _run(runner, event)
    _assert_retained(event, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks", [
    ['data: [DONE]\n\n'],
    ['data: {"choices":[{"delta":{"content":"handled"}}]}\n\n'],
], ids=["terminal-ack", "handled-partial-response"])
async def test_successful_proxy_handling_retires_queue_checkpoint(tmp_path, monkeypatch, chunks):
    runner, event = _queued_turn(tmp_path, monkeypatch, "http://proxy.invalid")
    with _patch_aiohttp(_FakeSession(_FakeSSEResponse(sse_chunks=chunks))):
        result = await _run(runner, event)
    assert result["api_calls"] == 1
    assert not event._test_checkpoint_path.exists()
