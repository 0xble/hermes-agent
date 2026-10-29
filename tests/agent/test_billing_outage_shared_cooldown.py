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
        (503, "auth_unavailable: no auth available (providers=claude; last upstream error: "
         "overloaded_error: upstream at capacity)", "overloaded"),
        (529, "The server is overloaded, please try again later", "overloaded"),
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
     (500, "upstream at capacity"),
     (502, "upstream at capacity"),
     (503, "maximum context length exceeded"),
     (503, "invalid_request_error: prompt is too long: 210000 tokens > 200000 maximum"),
     (503, "auth_unavailable: no auth available; last upstream error: "
      "invalid_request_error: prompt is too long: 210000 tokens > 200000 maximum"),
     (503, "request validation failed: unsupported parameter temperature")],
)
def test_request_specific_or_unclassified_failures_do_not_arm_shared_cooldown(
    tmp_path, monkeypatch, status, message,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "agent:\n  api_max_retries: 0\n  auto_recovery_cycles: 0\ncompression:\n  enabled: false\n",
        encoding="utf-8",
    )
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


def test_retryable_rate_limit_arms_once_without_fallback_when_shared_write_fails(
    tmp_path, monkeypatch,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "agent:\n  api_max_retries: 3\n  auto_recovery_cycles: 0\ncompression:\n  enabled: false\n",
        encoding="utf-8",
    )
    child = harness.CHILD.replace("fallback_model=fallback", "fallback_model=[]")
    # Keep real classification, routing and local cooldown/backoff arithmetic. Fail only
    # the durable write and replace waiting, so this tests repeated real API attempts.
    child = (
        'from unittest.mock import patch\n'
        'patch("agent.shared_primary_cooldown.arm_cooldown", '
        'side_effect=OSError("shared cooldown directory is unwritable")).start()\n'
        'patch("agent.turn_api_error.interruptible_backoff_sleep", return_value=None).start()\n'
        + child
    ).replace(
        '"notices": notices,',
        '"notices": notices, "backoff_count": agent._rate_limit_backoff_count,',
    )
    monkeypatch.setattr(harness, "CHILD", child)
    with harness.StubProvider() as stub:
        stub.failure_status = 429
        stub.retry_after = None
        result = harness.run_turn(home, stub.url, "nonstream")
        primary_requests = stub.primary_requests()
    assert result["failed"] is True
    assert len(primary_requests) >= 3, "exercise multiple retries of the rate-limited turn"
    assert result["backoff_count"] == 1, "local backoff must count turns, not retries"
    assert not (home / "state" / "model_cooldowns.json").exists()


def test_unarmed_rate_limit_leaves_terminal_exhaustion_its_short_window(monkeypatch):
    # An active fallback rate-limited after the chain is spent arms no primary cooldown.
    # The per-turn guard must stay clear, so terminal exhaustion still runs
    # _fallback_chain_exhausted and its short window gates the next turn's primary probe.
    from types import SimpleNamespace
    from unittest.mock import Mock
    from agent import fallback_cooldown, turn_recovery
    from agent.error_classifier import ClassifiedError, FailoverReason
    from agent.turn_retry_state import TurnRetryState

    arm = Mock(return_value=None)
    monkeypatch.setattr(fallback_cooldown, "_arm_rate_limit_cooldown", arm)
    retry = TurnRetryState()
    agent = SimpleNamespace(_fallback_index=1, _fallback_chain=[{}], _credential_pool=None)
    classified = ClassifiedError(reason=FailoverReason.upstream_rate_limit, status_code=429)
    try:
        turn_recovery.route_classified_error(
            agent, Exception("429"), classified, retry, error_msg="429", error_context={},
            recovered_with_pool=False, base_url="https://backup.invalid", model="backup",
            messages=[], api_messages=[], system_message=None, active_system_prompt="",
            conversation_history=[], retry_count=0, max_retries=3, compression_attempts=0,
            max_compression_attempts=1, api_call_count=1, effective_task_id="t",
        )
    except Exception:
        pass  # Only the guard state before later routing steps matters here.
    arm.assert_called_once()
    assert retry.shared_cooldown_arm_attempted is False


def test_deferred_fallback_switch_still_arms_the_shared_breaker(monkeypatch):
    # With fallback.min_switch_reset_seconds set and a near reset, try_activate_fallback stays on
    # the primary and returns before arming. The outage must still reach the shared breaker so
    # other processes stop probing the cooled primary.
    from types import SimpleNamespace
    from unittest.mock import Mock
    from agent import fallback_cooldown, turn_recovery
    from agent.error_classifier import ClassifiedError, FailoverReason
    from agent.turn_retry_state import TurnRetryState

    arm = Mock(return_value=30)
    monkeypatch.setattr(fallback_cooldown, "_arm_rate_limit_cooldown", arm)
    monkeypatch.setattr(fallback_cooldown, "switch_deferred_by_reset", Mock(return_value=True))
    retry = TurnRetryState()
    agent = SimpleNamespace(_fallback_index=0, _fallback_chain=[{"provider": "x", "model": "y"}],
                            _credential_pool=None)
    classified = ClassifiedError(reason=FailoverReason.upstream_rate_limit, status_code=429)
    try:
        turn_recovery.route_classified_error(
            agent, Exception("429"), classified, retry, error_msg="429", error_context={"reset_at": 30},
            recovered_with_pool=False, base_url="https://primary.invalid", model="primary",
            messages=[], api_messages=[], system_message=None, active_system_prompt="",
            conversation_history=[], retry_count=0, max_retries=3, compression_attempts=0,
            max_compression_attempts=1, api_call_count=1, effective_task_id="t",
        )
    except Exception:
        pass  # Only the arming decision before later routing steps matters here.
    arm.assert_called_once()
    assert retry.shared_cooldown_arm_attempted is True
