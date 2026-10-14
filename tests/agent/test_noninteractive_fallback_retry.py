"""Non-interactive provider failover policy.

Unattended work gets a bounded chance to recover a transient route before consuming the next
fallback. Interactive chat retains the existing immediate-fallback behavior, and billing walls
remain immediate regardless of surface.
"""

from types import SimpleNamespace

from agent.error_classifier import FailoverReason
from agent.fallback_cooldown import (
    DEFAULT_NONINTERACTIVE_TRANSIENT_RETRY_WINDOW_SECONDS,
    hold_noninteractive_transient_retry,
    is_noninteractive_session,
    noninteractive_transient_retry_window_seconds,
)


def _classified(reason, *, retryable=True):
    return SimpleNamespace(reason=reason, retryable=retryable)


def _agent(platform, *, window=None):
    agent = SimpleNamespace(platform=platform, side_agent=False, provider="p", model="m", base_url="u")
    if window is not None:
        agent._noninteractive_transient_retry_window_seconds = window
    return agent


def test_two_minute_transient_outage_stays_on_provider_two_without_reaching_provider_three():
    agent = _agent("subagent")
    error = _classified(FailoverReason.server_error)

    # The existing retry/backoff loop calls the policy before each fallback decision.  During a
    # two-minute outage it must keep retrying the active route instead of advancing the chain.
    assert hold_noninteractive_transient_retry(agent, error, now=0.0) is True
    assert hold_noninteractive_transient_retry(agent, error, now=120.0) is True
    assert hold_noninteractive_transient_retry(agent, error, now=180.0) is False


def test_interactive_chat_keeps_immediate_transient_fallback():
    agent = _agent("cli")
    assert is_noninteractive_session(agent) is False
    assert hold_noninteractive_transient_retry(
        agent, _classified(FailoverReason.rate_limit), now=0.0
    ) is False


def test_billing_and_entitlement_errors_move_on_immediately_for_unattended_work():
    agent = _agent("cron")
    for reason in (FailoverReason.billing, FailoverReason.auth_permanent):
        assert hold_noninteractive_transient_retry(agent, _classified(reason, retryable=False), now=0.0) is False


def test_delegation_fallback_window_has_three_minute_default_and_allows_zero_override():
    assert DEFAULT_NONINTERACTIVE_TRANSIENT_RETRY_WINDOW_SECONDS == 180.0
    assert noninteractive_transient_retry_window_seconds(_agent("subagent")) == 180.0
    assert noninteractive_transient_retry_window_seconds(_agent("subagent", window=0)) == 0.0
