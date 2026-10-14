"""Non-interactive provider failover policy.

Unattended work gets a bounded chance to recover a transient route before consuming the next
fallback. Interactive chat retains the existing immediate-fallback behavior, and billing walls
remain immediate regardless of surface.

The integration tests drive the real ``run_conversation`` retry loop
(``handle_api_error`` -> ``route_classified_error`` -> ``settle_unrecovered_error``) against a
fake provider client and a fake clock: the backoff sleep advances the clock instead of waiting.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import FailoverReason
from agent.fallback_cooldown import (
    DEFAULT_NONINTERACTIVE_TRANSIENT_RETRY_WINDOW_SECONDS,
    hold_noninteractive_transient_retry,
    is_noninteractive_session,
    noninteractive_transient_retry_window_seconds,
)
from run_agent import AIAgent

PRIMARY = ("zai", "glm-5.1")
FALLBACK = ("deepseek", "deepseek-chat")
_FALLBACK_BASE = "https://api.deepseek.com/v1"
_BASE = "https://open.bigmodel.cn/api/coding/paas/v4"
WINDOW = 180.0


# --- unit: the window policy ---------------------------------------------------------------

def _classified(reason, *, retryable=True):
    return SimpleNamespace(reason=reason, retryable=retryable)


def _stub(platform, *, window=None):
    agent = SimpleNamespace(platform=platform, side_agent=False, provider="p", model="m", base_url="u")
    if window is not None:
        agent._noninteractive_transient_retry_window_seconds = window
    return agent


def test_window_holds_until_it_expires():
    agent = _stub("subagent", window=WINDOW)
    error = _classified(FailoverReason.server_error)
    assert hold_noninteractive_transient_retry(agent, error, now=0.0) is True
    assert hold_noninteractive_transient_retry(agent, error, now=120.0) is True
    assert hold_noninteractive_transient_retry(agent, error, now=180.0) is False


def test_interactive_chat_never_holds():
    agent = _stub("cli", window=WINDOW)
    assert is_noninteractive_session(agent) is False
    assert hold_noninteractive_transient_retry(agent, _classified(FailoverReason.rate_limit), now=0.0) is False


def test_billing_and_entitlement_errors_never_hold():
    agent = _stub("cron", window=WINDOW)
    for reason in (FailoverReason.billing, FailoverReason.auth_permanent):
        assert hold_noninteractive_transient_retry(agent, _classified(reason, retryable=False), now=0.0) is False


def test_route_owner_retry_wait_cap_opts_out_of_the_window():
    agent = _stub("subagent", window=WINDOW)
    agent._max_retry_wait_s = 60.0
    assert hold_noninteractive_transient_retry(agent, _classified(FailoverReason.server_error), now=0.0) is False


def test_delegation_fallback_window_has_three_minute_default_and_allows_zero_override():
    assert DEFAULT_NONINTERACTIVE_TRANSIENT_RETRY_WINDOW_SECONDS == 180.0
    assert noninteractive_transient_retry_window_seconds(_stub("subagent")) == 180.0
    assert noninteractive_transient_retry_window_seconds(_stub("subagent", window=0)) == 0.0


# --- integration: the real retry loop ------------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _ProviderError(Exception):
    def __init__(self, status: int, message: str, retry_after: str | None = None):
        super().__init__(f"Error code: {status} - {message}")
        self.status_code = status
        self.response = SimpleNamespace(headers={"retry-after": retry_after} if retry_after else {})
        self.body = {"error": {"message": message}}


def _unavailable(retry_after=None):
    return _ProviderError(503, "service unavailable", retry_after)


def _rate_limited():
    return _ProviderError(429, "rate limit exceeded")


def _billing():
    return _ProviderError(402, "insufficient credits, please add a payment method")


def _ok(content):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="m", usage=None)


def _agent(platform: str) -> AIAgent:
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key-abcdef12", base_url=_BASE, provider=PRIMARY[0], model=PRIMARY[1],
            platform=platform, quiet_mode=True, skip_context_files=True, skip_memory=True,
            fallback_model=[{"provider": FALLBACK[0], "model": FALLBACK[1], "base_url": _FALLBACK_BASE}],
        )
    agent.client = MagicMock()
    agent._api_max_retries = 3
    agent._noninteractive_transient_retry_window_seconds = WINDOW
    return agent


def _drive(agent: AIAgent, provider_behaviour):
    """Run one turn. ``provider_behaviour(route, clock_now)`` returns a response or raises.

    Each provider call costs one fake second and each backoff advances the clock by its wait, so
    the window always elapses. The agent conftest zeroes ``jittered_backoff``; restore a
    deterministic exponential schedule so the loop paces like production."""
    clock = _Clock()
    calls: list[tuple[tuple[str, str], float]] = []
    sleeps: list[float] = []

    def fake_api_call(api_kwargs):
        route = (agent.provider, agent.model)
        calls.append((route, clock.now))
        clock.now += 1.0
        if len(calls) > 200:
            raise AssertionError("retry loop did not terminate")
        return provider_behaviour(route, clock.now)

    def fake_backoff(agent_, wait, *args, **kwargs):
        sleeps.append(wait)
        clock.now += wait
        return None

    fb_client = MagicMock()
    fb_client.api_key, fb_client.base_url = "fallback-key-abcdef12", _FALLBACK_BASE
    fb_client._custom_headers = fb_client.default_headers = None
    with (
        patch("agent.retry_utils.jittered_backoff", side_effect=lambda attempt, **k: min(2.0 ** attempt, 60.0)),
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.fallback_cooldown._monotonic", clock),
        patch("agent.turn_api_error.interruptible_backoff_sleep", side_effect=fake_backoff),
        patch("agent.turn_recovery.interruptible_backoff_sleep", side_effect=fake_backoff),
        patch("agent.agent_runtime_helpers.time.sleep"),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(fb_client, FALLBACK[1])),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("hello")
    return result, calls, sleeps


def _primary_down_fallback_up(error_factory):
    def behaviour(route, _now):
        if route == PRIMARY:
            raise error_factory()
        return _ok("served by fallback")
    return behaviour


def test_unattended_agent_retries_same_provider_inside_window_then_falls_back():
    agent = _agent("subagent")
    result, calls, _sleeps = _drive(agent, _primary_down_fallback_up(_unavailable))

    primary = [t for route, t in calls if route == PRIMARY]
    fallback = [t for route, t in calls if route == FALLBACK]
    assert result["completed"] is True and result["final_response"] == "served by fallback"
    # More same-provider attempts than the ordinary retry budget: the window kept the loop alive.
    assert len(primary) > agent._api_max_retries
    assert all(t - primary[0] < WINDOW for t in primary[:-1])
    # The fallback runs only once the window has elapsed, and exactly once.
    assert len(fallback) == 1 and fallback[0] - primary[0] >= WINDOW
    assert agent._fallback_activated is True and agent.model == FALLBACK[1]


def test_two_minute_outage_recovers_on_the_same_provider_without_fallback():
    agent = _agent("subagent")

    def behaviour(route, now):
        assert route == PRIMARY, "fallback must not be consumed during a short outage"
        if now < 1000.0 + 120.0:
            raise _unavailable()
        return _ok("primary recovered")

    result, calls, _sleeps = _drive(agent, behaviour)

    assert result["completed"] is True and result["final_response"] == "primary recovered"
    assert {route for route, _ in calls} == {PRIMARY}
    assert len(calls) > agent._api_max_retries
    assert agent._fallback_activated is False


@pytest.mark.parametrize(("error_factory", "max_primary_calls"), [(_rate_limited, 1), (_unavailable, 2)])
def test_interactive_chat_fails_over_immediately(error_factory, max_primary_calls):
    agent = _agent("cli")
    result, calls, _sleeps = _drive(agent, _primary_down_fallback_up(error_factory))

    primary = [t for route, t in calls if route == PRIMARY]
    assert result["final_response"] == "served by fallback"
    assert len(primary) <= max_primary_calls
    assert primary[-1] - primary[0] < 60


def test_unattended_billing_error_fails_over_immediately():
    agent = _agent("cron")
    result, calls, sleeps = _drive(agent, _primary_down_fallback_up(_billing))

    assert result["final_response"] == "served by fallback"
    assert [route for route, _ in calls] == [PRIMARY, FALLBACK]
    assert sleeps == []


def test_route_owner_retry_wait_cap_skips_the_window():
    """A delegate route owner's ``max_retry_wait_seconds`` opt-out wins: no same-provider hold."""
    agent = _agent("subagent")
    agent._max_retry_wait_s = 60.0
    result, calls, sleeps = _drive(agent, _primary_down_fallback_up(lambda: _unavailable(retry_after="600")))

    assert [route for route, _ in calls] == [PRIMARY, FALLBACK]
    assert sleeps == []
    assert result["final_response"] == "served by fallback"


def test_stale_outage_from_an_earlier_turn_does_not_cancel_the_next_window():
    agent = _agent("subagent")
    # A previous failed turn left an outage start for this exact route long in the past.
    agent._noninteractive_transient_retry_route = (PRIMARY[0], PRIMARY[1], str(agent.base_url))
    agent._noninteractive_transient_retry_started_at = -10_000.0

    def behaviour(route, now):
        assert route == PRIMARY, "a stale outage start must not trigger an immediate fallback"
        if now < 1000.0 + 60.0:
            raise _unavailable()
        return _ok("primary recovered")

    result, calls, _sleeps = _drive(agent, behaviour)

    assert result["final_response"] == "primary recovered"
    assert {route for route, _ in calls} == {PRIMARY}


def test_declared_cooldown_longer_than_the_window_falls_back_without_holding():
    """A provider that says it is out for two hours cannot recover inside the window: move on."""
    agent = _agent("cron")
    result, calls, sleeps = _drive(agent, _primary_down_fallback_up(lambda: _ProviderError(429, "rate limit exceeded", "7200")))

    assert result["final_response"] == "served by fallback"
    assert [route for route, _ in calls] == [PRIMARY, FALLBACK]
    assert sleeps == []
