"""Billing outages must not enter the generic 5xx retry/backoff path.

The local CLIProxyAPI returns 503 ``auth_unavailable`` envelopes after an
upstream credential falls through to an empty Anthropic billing account.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from agent.error_classifier import FailoverReason, classify_api_error
from agent.turn_recovery import nonretryable_client_error_result


class _ProxyError(Exception):
    status_code = 503

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.body = {"type": "api_error", "message": message}


def test_auth_unavailable_credit_balance_is_billing_not_server_error():
    error = _ProxyError(
        "auth_unavailable: no auth available (providers=claude, model=claude-opus-5-5; "
        "last upstream error: invalid_request_error: Your credit balance is too low "
        "to access the Anthropic API)"
    )

    classified = classify_api_error(error, provider="custom:claude-proxy", model="claude-opus-5-5")

    assert classified.reason is FailoverReason.billing
    assert classified.retryable is False


def test_provider_wide_proxy_rate_limit_arms_shared_cooldown(tmp_path, monkeypatch):
    from agent.fallback_cooldown import _arm_rate_limit_cooldown
    from agent.shared_primary_cooldown import read_cooldown, route_from_agent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    error = _ProxyError(
        "auth_unavailable: no auth available; last upstream error: rate_limit_error: "
        "This request would exceed your account's rate limit"
    )
    classified = classify_api_error(error, provider="custom:claude-proxy", model="claude-opus-5-5")
    agent = SimpleNamespace(
        log_prefix="", provider="custom:claude-proxy", model="claude-opus-5-5",
        base_url="http://127.0.0.1:8317/v1", _fallback_activated=False,
    )

    assert classified.reason is FailoverReason.rate_limit
    assert _arm_rate_limit_cooldown(agent, classified.reason) is not None
    assert read_cooldown(route_from_agent(agent))["reason"] == "rate_limit"


class _Agent:
    log_prefix = ""
    verbose_logging = False

    def _summarize_api_error(self, error):
        return str(error)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def test_classified_billing_400_persists_a_large_failed_session():
    error = _ProxyError("Your credit balance is too low to access the Anthropic API")
    error.status_code = 400
    classified = classify_api_error(error, provider="custom:claude-proxy", model="claude-opus-5-5")
    agent = _Agent()
    agent._persist_session = Mock()

    nonretryable_client_error_result(
        agent,
        error,
        classified,
        status_code=400,
        api_kwargs=None,
        api_messages=[{}] * 81,
        messages=[{"role": "user", "content": "continue"}],
        conversation_history=[{}] * 60,
        api_call_count=1,
        approx_tokens=100_000,
        provider="custom:claude-proxy",
        base_url="http://127.0.0.1:8317/v1",
        model="claude-opus-5-5",
    )

    assert classified.reason is FailoverReason.billing
    agent._persist_session.assert_called_once()



def test_billing_cooldown_is_adopted_by_a_fresh_agent_without_primary_probe(tmp_path):
    """A separate process adopts the classified billing outage on its fallback route."""
    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / ".hermes")
    env["PYTHONPATH"] = str(root)
    code = r'''
import json
from unittest.mock import MagicMock, patch
from agent.error_classifier import classify_api_error
from agent.fallback_cooldown import _arm_rate_limit_cooldown
from run_agent import AIAgent

class ProxyError(Exception):
    status_code = 503
    def __init__(self, message):
        super().__init__(message)
        self.body = {"type": "api_error", "message": message}

message = (
    "auth_unavailable: no auth available; last upstream error: "
    "invalid_request_error: Your credit balance is too low"
)
classified = classify_api_error(ProxyError(message), provider="custom:claude-proxy", model="claude-opus-5-5")
with patch("model_tools.get_tool_definitions", return_value=[]), patch("model_tools.check_toolset_requirements", return_value={}), patch("agent.process_bootstrap.OpenAI"):
    first = AIAgent(
        api_key="primary-key", base_url="https://primary.invalid/v1", provider="openrouter",
        model="primary-model", api_mode="chat_completions", quiet_mode=True,
        skip_context_files=True, skip_memory=True,
        fallback_model=[{"provider": "openai", "model": "fallback-model", "base_url": "https://fallback.invalid/v1"}],
    )
    armed = _arm_rate_limit_cooldown(first, classified.reason)
    with patch("agent.auxiliary_client.resolve_provider_client") as resolve:
        client = MagicMock()
        client.base_url = "https://fallback.invalid/v1"
        client.api_key = "fallback-key"
        resolve.return_value = (client, "fallback-model")
        second = AIAgent(
            api_key="primary-key", base_url="https://primary.invalid/v1", provider="openrouter",
            model="primary-model", api_mode="chat_completions", quiet_mode=True,
            skip_context_files=True, skip_memory=True,
            fallback_model=[{"provider": "openai", "model": "fallback-model", "base_url": "https://fallback.invalid/v1"}],
        )
print(json.dumps({
    "reason": classified.reason.value,
    "armed": armed is not None,
    "second_provider": second.provider,
    "second_model": second.model,
    "fallback_client_calls": resolve.call_count,
}))
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root, env=env, check=True,
        capture_output=True, text=True,
    )
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome == {
        "reason": "billing",
        "armed": True,
        "second_provider": "openai",
        "second_model": "fallback-model",
        "fallback_client_calls": 1,
    }
