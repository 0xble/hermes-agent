"""Classified account failures persist long turns and outrank generic proxy 5xx verdicts."""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from agent.error_classifier import ClassifiedError, FailoverReason, classify_api_error
from agent.turn_recovery import nonretryable_client_error_result


class _ProxyError(Exception):
    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = {"type": "api_error", "message": message}


@pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
@pytest.mark.parametrize(
    ("upstream", "reason"),
    [
        ("invalid_request_error: Your credit balance is too low to access the Anthropic API", FailoverReason.billing),
        ("rate_limit_error: This request would exceed your account's rate limit", FailoverReason.rate_limit),
    ],
)
def test_proxy_auth_unavailable_uses_named_upstream_reason(status, upstream, reason):
    error = _ProxyError(
        "auth_unavailable: no auth available (providers=claude, model=claude-opus-5-5; "
        f"last upstream error: {upstream})", status,
    )
    classified = classify_api_error(error, provider="custom:claude-proxy", model="claude-opus-5-5")
    assert classified.reason is reason
    assert classified.should_fallback is True
    assert classified.retryable is (reason is FailoverReason.rate_limit)


@pytest.mark.parametrize(
    "message",
    [
        "credit balance too low",  # a generic 5xx cannot establish an upstream account failure
        "auth_unavailable: no auth available; last upstream error: unrelated internal error",
        "auth_unavailable: no auth available; billing in wrapper; last upstream error: unrelated internal error",
    ],
)
def test_unclassified_5xx_wrapper_is_not_relabelled_as_an_account_failure(message):
    classified = classify_api_error(_ProxyError(message, 500), provider="custom:claude-proxy")
    assert classified.reason is FailoverReason.server_error
    assert not classified.error_context.get("provider_wide")


@pytest.mark.parametrize(
    ("status", "message", "reason"),
    [
        (500, "Internal server error", FailoverReason.server_error),
        (503, "maximum context length exceeded", FailoverReason.context_overflow),
        (503, "request validation failed: unsupported parameter temperature", FailoverReason.format_error),
    ],
)
def test_request_specific_or_generic_5xx_does_not_mark_provider_wide(status, message, reason):
    classified = classify_api_error(_ProxyError(message, status))
    assert classified.reason is reason
    assert not classified.error_context.get("provider_wide")


class _Agent:
    log_prefix = ""
    verbose_logging = False

    def _summarize_api_error(self, error):
        return str(error)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.mark.parametrize(
    "reason",
    [FailoverReason.billing, FailoverReason.auth, FailoverReason.rate_limit,
     FailoverReason.content_policy_blocked, FailoverReason.format_error],
)
def test_classified_nonoverflow_400_persists_a_large_failed_session(reason):
    error = _ProxyError("HTTP 400 rejected request", 400)
    classified = ClassifiedError(reason=reason, status_code=400, retryable=False)
    agent = _Agent()
    agent._persist_session = Mock()
    nonretryable_client_error_result(
        agent, error, classified, status_code=400, api_kwargs=None, api_messages=[{}] * 81,
        messages=[{"role": "user", "content": "continue"}], conversation_history=[{}] * 60,
        api_call_count=1, approx_tokens=100_000, provider="custom:claude-proxy",
        base_url="http://127.0.0.1:8317/v1", model="claude-opus-5-5",
    )
    agent._persist_session.assert_called_once()
