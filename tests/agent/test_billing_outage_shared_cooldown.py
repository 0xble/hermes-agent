"""Known provider/account outages share a breaker across real conversation processes."""
from __future__ import annotations

import json

import pytest

from tests.agent import _shared_cooldown_stub as harness


@pytest.mark.parametrize(
    ("status", "message", "reason"),
    [
        (503, "auth_unavailable: no auth available (providers=claude; last upstream error: "
         "invalid_request_error: Your credit balance is too low to access the Anthropic API)", "billing"),
        (503, "auth_unavailable: no auth available (providers=claude; last upstream error: "
         "rate_limit_error: This request would exceed your account's rate limit)", "rate_limit"),
        (400, "Your credit balance is too low to access the Anthropic API", "billing"),
        (503, "The server is overloaded, please try again later", "overloaded"),
        (500, "The server is overloaded, please try again later", "overloaded"),
    ],
)
def test_provider_outage_is_shared_without_backoff_or_fresh_agent_primary_probe(
    tmp_path, monkeypatch, status, message, reason,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "agent:\n  api_max_retries: 3\ncompression:\n  enabled: false\n", encoding="utf-8",
    )
    monkeypatch.setitem(harness._FAILURE_BODIES, status, {"type": "api_error", "message": message})
    # Exercise the normal retry budget without ever spending a 600s provider wait in a regression.
    # Calling this sleeper is itself a test failure, not a synthetic provider response.
    monkeypatch.setattr(
        harness, "CHILD",
        'from unittest.mock import patch\n'
        'patch("agent.turn_api_error.interruptible_backoff_sleep", '
        'side_effect=AssertionError("provider-wide outage entered local backoff")).start()\n'
        + harness.CHILD,
    )
    with harness.StubProvider() as stub:
        stub.failure_status = status
        stub.retry_after = None
        first = harness.run_turn(home, stub.url, "nonstream")
        primary_after_a = list(stub.primary_requests())
        record_path = home / "state" / "model_cooldowns.json"
        records = json.loads(record_path.read_text(encoding="utf-8-sig"))["routes"]
        (record,) = records.values()
        second = harness.run_turn(home, stub.url, "nonstream")
        primary_after_b = list(stub.primary_requests())

    assert first["final_response"] == "OK from fallback-model"
    assert first["failed"] is False
    assert len(primary_after_a) == 1, "agent A must fall back before local retry/backoff"
    assert record["reason"] == reason
    assert record["model"] == "primary-model"
    assert second["final_response"] == "OK from fallback-model"
    assert second["model"] == "fallback-model"
    assert primary_after_b == primary_after_a, "fresh agent B must never probe the cooled primary"


@pytest.mark.parametrize(
    ("status", "message"),
    [(500, "Internal server error"),
     (503, "maximum context length exceeded"),
     (503, "request validation failed: unsupported parameter temperature")],
)
def test_request_specific_or_unclassified_failures_do_not_arm_shared_cooldown(
    tmp_path, monkeypatch, status, message,
):
    home = tmp_path / ".hermes"
    harness.write_home_config(home)  # zero retries; no real provider backoff in the regression
    monkeypatch.setitem(harness._FAILURE_BODIES, status, {"type": "api_error", "message": message})
    with harness.StubProvider() as stub:
        stub.failure_status = status
        stub.retry_after = None
        harness.run_turn(home, stub.url, "nonstream")
    assert not (home / "state" / "model_cooldowns.json").exists()


@pytest.mark.parametrize(
    ("status", "message", "reason"),
    [(400, "Your credit balance is too low", "billing")],
)
def test_known_outage_arms_even_when_agent_a_has_no_fallback_chain(
    tmp_path, monkeypatch, status, message, reason,
):
    home = tmp_path / ".hermes"
    harness.write_home_config(home)
    original_run = harness.subprocess.run

    def bounded_run(*args, **kwargs):
        kwargs["timeout"] = 20
        return original_run(*args, **kwargs)

    monkeypatch.setattr(harness.subprocess, "run", bounded_run)
    monkeypatch.setitem(harness._FAILURE_BODIES, status, {"type": "api_error", "message": message})
    monkeypatch.setattr(harness, "CHILD", harness.CHILD.replace("fallback_model=fallback", "fallback_model=[]"))
    with harness.StubProvider() as stub:
        stub.failure_status = status
        stub.retry_after = None
        result = harness.run_turn(home, stub.url, "nonstream")
    assert result["failed"] is True
    records = json.loads((home / "state" / "model_cooldowns.json").read_text(encoding="utf-8-sig"))["routes"]
    (record,) = records.values()
    assert record["reason"] == reason
