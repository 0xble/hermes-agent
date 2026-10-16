"""Proxy-relayed transport resets arrive as SSE ``event: error`` frames, which the
Anthropic SDK raises as an HTTP-200 ``APIStatusError``. They must count as transient
so a mid-tool-call drop is retried instead of stubbed."""
import httpx
import pytest

anthropic = pytest.importorskip("anthropic")

from agent.chat_completion_helpers import _is_sse_connection_error


def _anthropic_status_error(message: str, status: int):
    request = httpx.Request("POST", "http://127.0.0.1:8317/v1/messages")
    response = httpx.Response(status, request=request)
    body = {"type": "error", "error": {"type": "api_error", "message": message}}
    return anthropic.APIStatusError(str(body), response=response, body=body)


def test_proxy_relayed_connection_reset_frame_is_transient():
    err = _anthropic_status_error(
        "read tcp 192.168.1.183:50946->160.79.104.10:443: read: connection reset by peer", 200)
    assert _is_sse_connection_error(err)


def test_real_http_error_with_transport_wording_is_not_transient():
    assert not _is_sse_connection_error(_anthropic_status_error("connection reset by peer", 400))


def test_sse_error_frame_without_transport_wording_is_not_transient():
    assert not _is_sse_connection_error(_anthropic_status_error("invalid tool schema", 200))


def test_unrelated_exception_is_not_transient():
    assert not _is_sse_connection_error(RuntimeError("connection reset by peer"))
