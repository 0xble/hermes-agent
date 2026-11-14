"""Definitive outages fall back without a duplicate non-streaming primary request."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError

from agent.error_classifier import classify_api_error

from agent import chat_completion_helpers as h
from tests.agent import _shared_cooldown_stub as harness
from tests.agent.test_stream_unmask_5xx import _make_call, _StreamErr, _ProbeErr


def _child_at(now, child):
    # Wall time drives the durable breaker; monotonic time still drives real request
    # deadlines. Both processes must observe an active window regardless of startup
    # latency on a contended CI runner.
    return (
        'from unittest.mock import patch\n'
        f'patch("time.time", return_value={now!r}).start()\n'
        + child
    )


@pytest.mark.parametrize(
    ("status", "message", "reason"),
    [
        (503, "auth_unavailable: no auth available; last upstream error: "
         "invalid_request_error: Your credit balance is too low to access the Anthropic API", "billing"),
        (503, "auth_unavailable: no auth available; last upstream error: "
         "rate_limit_error: This request would exceed your account's rate limit", "rate_limit"),
        (429, "rate_limit_error: retry later", "rate_limit"),
        (503, "auth_unavailable: no auth available; last upstream error: "
         "overloaded_error: upstream at capacity", "overloaded"),
        (529, "The server is overloaded, please try again later", "overloaded"),
    ],
)
def test_stream_outage_falls_back_after_one_primary_call(tmp_path, monkeypatch, status, message, reason):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "agent:\n  api_max_retries: 3\ncompression:\n  enabled: false\n", encoding="utf-8",
    )
    body = {"error": {"type": "api_error", "message": message}}
    monkeypatch.setitem(harness._FAILURE_BODIES, status, body)
    error = APIStatusError(message, body=body, response=httpx.Response(
        status, request=httpx.Request("POST", "http://127.0.0.1/v1/chat/completions"),
    ))
    assert classify_api_error(error, provider="custom").reason.value == reason
    monkeypatch.setattr(
        harness, "CHILD",
        _child_at(1_900_000_000.0,
            'from unittest.mock import patch\n'
            'patch("agent.turn_api_error.interruptible_backoff_sleep", '
            'side_effect=AssertionError("provider-wide outage entered local backoff")).start()\n'
            + harness.CHILD,
        ),
    )
    with harness.StubProvider() as stub:
        stub.failure_status = status
        stub.retry_after = None
        first = harness.run_turn(home, stub.url, "stream")
        first_primary = list(stub.primary_requests())
        second = harness.run_turn(home, stub.url, "stream")
        all_primary = list(stub.primary_requests())
    assert first["final_response"] == second["final_response"] == "OK from fallback-model"
    assert first["failed"] is second["failed"] is False
    assert first_primary == [{"model": "primary-model", "status": status, "stream": True}], (
        "a definitive outage must reach fallback after 1 primary call, without an unmask probe", first_primary,
    )
    assert all_primary == first_primary, "a fresh process must not call the cooled primary"
    (record,) = json.loads((home / "state" / "model_cooldowns.json").read_text(encoding="utf-8-sig"))["routes"].values()
    assert record["reason"] == reason


def test_transient_stream_outage_allows_one_primary_probe_after_short_window(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "agent:\n  api_max_retries: 3\ncompression:\n  enabled: false\n", encoding="utf-8",
    )
    child = harness.CHILD
    now = 1_900_000_000.0
    with harness.StubProvider() as stub:
        stub.failure_status = 429
        stub.retry_after = None
        monkeypatch.setattr(harness, "CHILD", _child_at(now, child))
        first = harness.run_turn(home, stub.url, "stream")
        (initial,) = json.loads((home / "state" / "model_cooldowns.json").read_text(encoding="utf-8-sig"))["routes"].values()
        assert initial["reset_at"] - initial["recorded_at"] == 15
        assert stub.primary_requests() == [{"model": "primary-model", "status": 429, "stream": True}]

        # A slow startup can legitimately cross the first window. That fresh
        # process owns a recovery probe, not a duplicate non-streaming unmask call.
        monkeypatch.setattr(harness, "CHILD", _child_at(now + 16, child))
        second = harness.run_turn(home, stub.url, "stream")
        assert stub.primary_requests() == [
            {"model": "primary-model", "status": 429, "stream": True},
            {"model": "primary-model", "status": 429, "stream": True},
        ]
    assert first["final_response"] == second["final_response"] == "OK from fallback-model"
    assert first["failed"] is second["failed"] is False
    (rearmed,) = json.loads((home / "state" / "model_cooldowns.json").read_text(encoding="utf-8-sig"))["routes"].values()
    assert rearmed["outage_id"] == initial["outage_id"]
    assert rearmed["backoff_count"] == 2
    assert rearmed["reset_at"] == now + 16 + 120


@pytest.mark.parametrize(("status", "upstream_limit"), [(500, False), (502, False), (503, False), (503, True)])
@pytest.mark.parametrize("probe_status", [200, 400])
def test_probe_preserves_opaque_validation_but_skips_classified_upstream_limit(
    monkeypatch, status, upstream_limit, probe_status,
):
    call = _make_call({"model": "m", "messages": [], "stream": True})
    original = _StreamErr(status)
    hidden = _ProbeErr(400)
    probes = []
    if upstream_limit:
        # Provider plugins may classify a relay's 5xx as an upstream limit. Use a real
        # classifier result at that seam, not a probe-side message matcher.
        from agent import chat_completion_stream_probe
        upstream = APIStatusError("Provider returned error", body={"error": {
            "message": "Provider returned error",
            "metadata": {"provider_name": "DeepSeek", "raw": "Rate limit exceeded"},
        }}, response=httpx.Response(429, request=httpx.Request("POST", "http://127.0.0.1/v1")))
        classified = classify_api_error(upstream, provider="openrouter")
        assert classified.reason.value == "upstream_rate_limit"
        monkeypatch.setattr(chat_completion_stream_probe, "classify_api_error", lambda *args, **kwargs: classified)

    def probe(agent, kwargs):
        probes.append(kwargs)
        if probe_status == 400:
            raise hidden
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", reasoning_content=None))])

    monkeypatch.setattr(h, "interruptible_api_call", probe)
    assert call._handle_stream_error(original, attempt=0, max_retries=2) is False
    if upstream_limit:
        assert probes == []
        assert call.result["error"] is original
        assert call.result["response"] is None
        assert call.agent._stream_5xx_probe_ts is None
        assert call.agent._disable_streaming is False
        return
    assert probes == [{"model": "m", "messages": []}]
    if probe_status == 400:
        assert call.result["error"] is hidden
        assert call.result["response"] is None
    else:
        assert call.result["error"] is None
        assert call.deltas == ["ok"]
    assert call.agent._disable_streaming is False
    stamp = call.agent._stream_5xx_probe_ts
    assert stamp is not None
    assert call._unmask_server_error_with_nonstreaming(original) is False
    assert call.agent._stream_5xx_probe_ts == stamp
    assert probes == [{"model": "m", "messages": []}]
