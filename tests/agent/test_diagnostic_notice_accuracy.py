"""Diagnostic notices must not state a wrong cause or a past deadline (2026-11-14 audit).

- A refused connection during a proxy restart was reported as "request 771 KB ... may reject
  requests this large": the body was never sent, so its size cannot be the cause.
- A fallback notice said "gpt-6.1-sol is rate-limited until 09:06 PST" at 09:09.
"""

import time
from types import SimpleNamespace

import httpx
import openai
import pytest

from agent.chat_completion_helpers import _rate_limit_fallback_notice
from agent.error_classifier import FailoverReason
from agent.stream_diag import connect_exhausted_notice

_BIG = b"x" * (800 * 1024)


def _chained(inner: BaseException) -> BaseException:
    request = httpx.Request("POST", "http://127.0.0.1:8317/v1/responses", content=_BIG)
    inner.request = request
    outer = openai.APIConnectionError(request=request)
    outer.__cause__ = inner
    return outer


def test_refused_connection_gets_no_request_size_blame():
    error = _chained(httpx.ConnectError("[Errno 61] Connection refused"))
    notice = connect_exhausted_notice(error, attempts=2, base_url=None)
    assert "127.0.0.1" in notice and "800 KB" in notice
    assert "reject requests this large" not in notice
    assert "unreachable" in notice


def test_reset_after_the_connection_opened_keeps_the_size_hint():
    error = _chained(httpx.RemoteProtocolError("Server disconnected without sending a response."))
    assert "reject requests this large" in connect_exhausted_notice(error, attempts=2, base_url=None)


@pytest.fixture()
def shared_outage(monkeypatch):
    import agent.shared_primary_cooldown as spc
    monkeypatch.setattr(spc, "route_from_record", lambda record: ("p", "m"))
    monkeypatch.setattr(spc, "claim_outage_notice", lambda route, outage_id, fallback: True)

    def agent_with(reset_at):
        return SimpleNamespace(_shared_primary_cooldown_record={
            "outage_id": "o1", "reset_at": reset_at, "reason": FailoverReason.rate_limit.value})
    return agent_with


def test_fallback_notice_with_a_past_reset_does_not_claim_active_recovery(shared_outage):
    agent = shared_outage(time.time() - 180)
    use_generic, notice = _rate_limit_fallback_notice(
        agent, FailoverReason.rate_limit, False, "gpt-6.1-sol", "claude-opus-5-5", "custom:claude-proxy")
    assert use_generic is False
    assert notice == ("⚠️ gpt-6.1-sol was rate-limited; using claude-opus-5-5 via "
                      "custom:claude-proxy while the primary is rechecked.")
    assert "until it is back" not in notice


def test_fallback_notice_with_a_future_reset_names_it(shared_outage):
    agent = shared_outage(time.time() + 3600)
    _, notice = _rate_limit_fallback_notice(
        agent, FailoverReason.rate_limit, False, "gpt-6.1-sol", "claude-opus-5-5", "custom:claude-proxy")
    assert notice.startswith("⚠️ gpt-6.1-sol is rate-limited until ") and notice.endswith("until then.")
