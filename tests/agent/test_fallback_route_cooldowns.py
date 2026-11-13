"""Fallback routes share the durable cooldown used by primary routes."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

from agent.error_classifier import FailoverReason
from agent.fallback_cooldown import _arm_rate_limit_cooldown
from agent.shared_primary_cooldown import arm_cooldown


PRIMARY = ("custom:primary", "https://primary.invalid/v1", "primary-model")
ROUTE_ONE = ("custom:one", "https://one.invalid/v1", "route-one")
ROUTE_TWO = ("custom:two", "https://two.invalid/v1", "route-two")


def _agent(chain):
    return SimpleNamespace(
        provider=PRIMARY[0], base_url=PRIMARY[1], model=PRIMARY[2],
        _primary_runtime={"provider": PRIMARY[0], "base_url": PRIMARY[1], "model": PRIMARY[2]},
        _fallback_activated=True, _fallback_chain=chain, _fallback_index=0,
        _rate_limit_backoff_count=0, _rate_limited_until=0,
        _fallback_episode_cooled=False, _shared_primary_cooldown_record=None,
        _entitlement_rejected_models=set(), _credential_pool=None,
    )


def _entry(route):
    return {"provider": route[0], "model": route[2], "base_url": route[1]}


def _skip(agent, entry):
    from agent.chat_completion_helpers import _should_skip_fallback_candidate

    with (
        patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None),
        patch("agent.fallback_route_cooldowns.candidate_pool_exhausted", return_value=False),
    ):
        return _should_skip_fallback_candidate(
            agent, entry, (entry["provider"], entry["model"]),
            entry["provider"], entry["model"], set(),
        )


def test_402_on_fallback_route_is_skipped_by_next_session():
    """A billing 402 on route two writes the shared record keyed to route two."""
    first = _agent([_entry(ROUTE_ONE), _entry(ROUTE_TWO)])
    first.provider, first.base_url, first.model = ROUTE_TWO
    assert _arm_rate_limit_cooldown(first, FailoverReason.billing) is not None

    second = _agent([_entry(ROUTE_ONE), _entry(ROUTE_TWO)])
    assert _skip(second, _entry(ROUTE_TWO)) is True
    assert _skip(second, _entry(ROUTE_ONE)) is False


def test_429_quota_exhaustion_with_60h_reset_skips_until_reset(monkeypatch):
    """A provider reset, even when 60 hours out, controls the shared skip window."""
    reset_at = time.time() + 60 * 60 * 60
    arm_cooldown(ROUTE_TWO, reason=FailoverReason.rate_limit, reset_at=reset_at)
    agent = _agent([_entry(ROUTE_ONE), _entry(ROUTE_TWO)])
    assert _skip(agent, _entry(ROUTE_TWO)) is True

    from agent import shared_primary_cooldown
    monkeypatch.setattr(shared_primary_cooldown.time, "time", lambda: reset_at + 1)
    assert _skip(agent, _entry(ROUTE_TWO)) is False


def test_when_all_fallbacks_are_cooled_the_soonest_reset_is_tried():
    """When every candidate is cooled, only the soonest-reset candidate remains eligible."""
    now = time.time()
    arm_cooldown(ROUTE_ONE, reason=FailoverReason.billing, reset_at=now + 2 * 60 * 60)
    arm_cooldown(ROUTE_TWO, reason=FailoverReason.billing, reset_at=now + 60 * 60)
    agent = _agent([_entry(ROUTE_ONE), _entry(ROUTE_TWO)])

    assert _skip(agent, _entry(ROUTE_ONE)) is True
    assert _skip(agent, _entry(ROUTE_TWO)) is False
