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
        self.body: dict = {"type": "api_error", "message": message}


@pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
@pytest.mark.parametrize(
    ("upstream", "reason"),
    [
        ("invalid_request_error: You're out of extra usage", FailoverReason.billing),
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
    if "out of extra usage" in upstream:
        assert classified.error_context["billing_unverified"] is True
        assert classified.error_context["possible_content_filter"] is True


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


@pytest.mark.parametrize("envelope", ["direct", "proxy"])
@pytest.mark.parametrize("status", [503, 529])
@pytest.mark.parametrize(
    "message",
    [
        "invalid_request_error: prompt is too long: 210000 tokens > 200000 maximum",
        "invalid_request_error: input length and max_tokens exceed context limit",
    ],
)
def test_proxy_and_anthropic_5xx_overflow_beats_validation(status, message, envelope):
    base = classify_api_error(_ProxyError(message, 400))
    body_message = (
        "auth_unavailable: no auth available (providers=claude; "
        f"last upstream error: {message})"
        if envelope == "proxy" else message
    )
    classified = classify_api_error(
        _ProxyError(body_message, status),
        provider="custom:claude-proxy" if envelope == "proxy" else "anthropic",
        model="claude-opus-5-5",
    )
    assert base.reason is FailoverReason.context_overflow
    assert classified.reason is base.reason
    assert classified.error_context.get("provider_wide") is not True


@pytest.mark.parametrize("status", [500, 502])
def test_plain_capacity_message_preserves_generic_5xx_classification(status):
    classified = classify_api_error(_ProxyError("upstream at capacity", status))
    assert classified.reason is FailoverReason.server_error
    assert not classified.error_context.get("provider_wide")


@pytest.mark.parametrize("status", [500, 502, 503])
@pytest.mark.parametrize("field", ["type", "code"])
def test_explicit_overloaded_error_code_is_provider_wide(status, field):
    error = _ProxyError("upstream at capacity", status)
    error.body = {"error": {field: "overloaded_error", "message": str(error)}}
    classified = classify_api_error(error)
    assert classified.reason is FailoverReason.overloaded
    assert classified.error_context["provider_wide"] is True


def test_proxy_provider_overload_signal_is_provider_wide():
    error = _ProxyError(
        "auth_unavailable: no auth available (providers=claude; "
        "last upstream error: overloaded_error: upstream at capacity)",
        503,
    )
    classified = classify_api_error(error, provider="custom:claude-proxy")
    assert classified.reason is FailoverReason.overloaded
    assert classified.error_context["provider_wide"] is True


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
     FailoverReason.content_policy_blocked],
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


@pytest.mark.parametrize(
    ("status", "message", "body"),
    [
        (503, "auth_unavailable: no auth available; last upstream error: "
              "invalid_request_error: This organization has been disabled.", None),
        (503, "Service temporarily unavailable", {"error": {"type": "invalid_request_error",
                                                             "message": "Service temporarily unavailable"}}),
        (529, "invalid_request_error: overloaded", None),
    ],
)
def test_capacity_status_with_bare_invalid_request_error_stays_retryable(status, message, body):
    # Proxies wrap account-level and capacity outages in a bare invalid_request_error on
    # 503/529. Only explicit parameter rejections are request-shape failures there.
    error = _ProxyError(message, status)
    if body is not None:
        error.body = body
    classified = classify_api_error(error, provider="custom:claude-proxy", model="claude-opus-5-5")
    assert classified.reason is FailoverReason.overloaded
    assert classified.retryable is True


@pytest.mark.parametrize("status", [503, 529])
def test_capacity_status_with_explicit_parameter_rejection_still_fails_fast(status):
    error = _ProxyError("invalid_request_error: unsupported parameter temperature", status)
    assert classify_api_error(error, provider="custom:claude-proxy").reason is FailoverReason.format_error
