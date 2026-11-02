"""Incident types describe the failure, not incidental auth or delivery context."""

import pytest

from cron import incidents


@pytest.mark.parametrize("error", [
    "[CRON_FAILURE]\n"
    "The owner wake was not delivered. Relay rejected the required key "
    "`slack-personal-consent:20261027:morning` with "
    "`invalid_key: expected a plain token of at most 200 characters`.\n\n"
    "I preserved the exact key rather than substituting another. Personal authentication "
    "matches the expected identity, but both Lists scopes remain absent. No consent, "
    "credential, browser, Slack, or configuration changes were made.",
    "invalid_key: expected a plain token of at most 200 characters",
    "Relay delivery rejected: invalid_title: title is required",
    "invalid_brief: expected a nonempty brief",
    "intent_required: specify an intent for relay delivery",
    "usage: relay send --key KEY --title TITLE --brief BRIEF",
    "self_relay: cannot relay to the sending session",
    "ValidationError: invalid argument for delivery",
    "Argument validation failed: authentication option is not supported",
])
def test_local_validation_incidents_are_config(error, monkeypatch, tmp_path):
    monkeypatch.setattr(incidents._executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    incident_id, is_new = incidents.upsert_incident("validation-job", error)

    assert is_new
    assert incidents.get_incident(incident_id)["failure_type"] == "config"


@pytest.mark.parametrize(("error", "expected"), [
    ('Slack API error: {"ok": false, "error": "not_authed"}', "auth"),
    ('Slack API error: {"ok": false, "error": "invalid_auth"}', "auth"),
    ('Slack API error: {"ok": false, "error": "token_expired"}', "auth"),
    ("OpenAI: Incorrect API key provided", "auth"),
    ("OpenAI: invalid API key", "auth"),
    ("Anthropic: authentication_error: invalid x-api-key", "auth"),
    ("openai.AuthenticationError: rejected credentials", "auth"),
    ("Slack auth failed", "auth"),
    ("Anthropic proxy: HTTP 503 auth_unavailable: no auth available", "auth"),
    ("Telegram getMe failed: HTTP 401 Unauthorized", "auth"),
    ("Provider returned HTTP 403 Forbidden", "auth"),
    ("authentication failed: invalid credentials", "auth"),
    ("credentials are invalid", "auth"),
    ("access token has expired", "auth"),
    ("authentication required", "auth"),
    ("delivery failed after authentication succeeded", "delivery"),
    ("author not found", "unknown"),
    ("expected a plain token for key", "unknown"),
    ("usage limit exceeded", "rate_limit"),
    ("HTTP 429 rate limit exceeded", "rate_limit"),
    ("Provider read timed out", "timeout"),
])
def test_auth_requires_a_failure_signal(error, expected):
    assert incidents._classify_failure_type(error) == expected
