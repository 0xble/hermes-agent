"""Fallback routes share the durable cooldown used by primary routes."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


@pytest.mark.parametrize(
    ("provider", "model", "stored_model"),
    [
        ("anthropic", "claude-sonnet-4.5", "claude-sonnet-4-5"),
        ("openrouter", "gpt-4o", "openai/gpt-4o"),
        ("openai-codex", "openai/gpt-5.4", "gpt-5.4"),
        ("deepseek", "deepseek-chat", "deepseek-flash"),
    ],
)
def test_lookup_normalizes_the_same_route_identity_as_live_binding(provider, model, stored_model):
    from agent import fallback_route_cooldowns

    entry = {"provider": provider, "model": stored_model, "base_url": "https://api.example/v1/"}
    with patch("agent.shared_primary_cooldown.list_cooldowns", return_value=[entry]):
        assert fallback_route_cooldowns.cooldown_entries(
            provider, model, "https://api.example/v1"
        ) == [entry]


def test_402_on_fallback_route_is_skipped_by_next_session():
    """A billing 402 on route two writes the shared record keyed to route two."""
    first = _agent([_entry(ROUTE_ONE), _entry(ROUTE_TWO)])
    first.provider, first.base_url, first.model = ROUTE_TWO
    assert _arm_rate_limit_cooldown(first, FailoverReason.billing) is None

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
    monkeypatch.setattr(shared_primary_cooldown, "_now", lambda: reset_at + 1)
    assert _skip(agent, _entry(ROUTE_TWO)) is False


def test_when_all_fallbacks_are_cooled_the_soonest_reset_is_tried():
    """When every candidate is cooled, only the soonest-reset candidate remains eligible."""
    now = time.time()
    arm_cooldown(ROUTE_ONE, reason=FailoverReason.billing, reset_at=now + 2 * 60 * 60)
    arm_cooldown(ROUTE_TWO, reason=FailoverReason.billing, reset_at=now + 60 * 60)
    agent = _agent([_entry(ROUTE_ONE), _entry(ROUTE_TWO)])

    assert _skip(agent, _entry(ROUTE_ONE)) is True
    assert _skip(agent, _entry(ROUTE_TWO)) is False


def _record(route):
    from agent.shared_primary_cooldown import get_cooldown
    return get_cooldown(route)


def test_primary_billing_without_reset_keeps_short_backoff():
    """P1-c: the 6h default is for verified fallback route records, never the primary."""
    agent = _agent([_entry(ROUTE_ONE)])
    agent._fallback_activated = False
    agent.provider, agent.base_url, agent.model = PRIMARY
    now = time.time()
    assert _arm_rate_limit_cooldown(agent, FailoverReason.billing) is not None
    record = _record(PRIMARY)
    assert record["source"] == "backoff"
    assert record["reset_at"] - now < 120


def test_unverified_billing_on_fallback_route_keeps_short_backoff():
    agent = _agent([_entry(ROUTE_ONE), _entry(ROUTE_TWO)])
    agent.provider, agent.base_url, agent.model = ROUTE_TWO
    now = time.time()
    _arm_rate_limit_cooldown(agent, FailoverReason.billing, billing_unverified=True)
    record = _record(ROUTE_TWO)
    assert record["source"] == "backoff"
    assert 59 <= record["reset_at"] - now < 120


def test_verified_billing_on_fallback_route_uses_long_default():
    agent = _agent([_entry(ROUTE_ONE), _entry(ROUTE_TWO)])
    agent.provider, agent.base_url, agent.model = ROUTE_TWO
    _arm_rate_limit_cooldown(agent, FailoverReason.billing)
    assert _record(ROUTE_TWO)["source"] == "billing_default"


def test_soonest_cooled_ignores_the_failed_current_backend():
    now = time.time()
    arm_cooldown(ROUTE_ONE, reason=FailoverReason.billing, reset_at=now + 3600)
    arm_cooldown(PRIMARY, reason=FailoverReason.rate_limit, reset_at=now + 60)
    agent = _agent([_entry(ROUTE_ONE), _entry(PRIMARY)])
    # The failed primary resets sooner but is a duplicate backend, not a usable alternative.
    assert _skip(agent, _entry(ROUTE_ONE)) is False
    assert _skip(agent, _entry(PRIMARY)) is True


ROUTE_THREE = ("custom:three", "https://three.invalid/v1", "route-three")


@pytest.mark.parametrize("reason", ["entitlement", "unavailable", "pool_exhausted", "not_local"])
def test_soonest_cooled_ignores_entries_that_would_be_skipped_anyway(reason):
    """P2-d: a candidate skipped regardless must not stop the soonest-reset pick."""
    from agent import fallback_route_cooldowns as frc
    from agent.chat_completion_helpers import _fallback_entry_key

    now = time.time()
    arm_cooldown(ROUTE_ONE, reason=FailoverReason.billing, reset_at=now + 3600)
    arm_cooldown(ROUTE_TWO, reason=FailoverReason.billing, reset_at=now + 7200)
    agent = _agent([_entry(ROUTE_ONE), _entry(ROUTE_TWO), _entry(ROUTE_THREE)])
    agent._fallback_index = 1
    args = (agent, ROUTE_ONE[0], ROUTE_ONE[2], ROUTE_ONE[1])
    # An uncooled usable third entry means the cooled route is not the last resort.
    assert frc.candidate_is_soonest_cooled(*args) is False

    patches = []
    if reason == "entitlement":
        agent._entitlement_rejected_models = {(ROUTE_THREE[0], ROUTE_THREE[2])}
    elif reason == "unavailable":
        agent._unavailable_fallback_keys = {_fallback_entry_key(_entry(ROUTE_THREE))}
    elif reason == "pool_exhausted":
        patches.append(patch.object(
            frc, "candidate_pool_exhausted", side_effect=lambda a, p, m: p == ROUTE_THREE[0]))
    else:
        patches.append(patch(
            "agent.chat_completion_helpers._fallback_entry_unavailable_without_network",
            side_effect=lambda a, fb: "nous_token_missing" if fb["provider"] == ROUTE_THREE[0] else None))
    for p in patches:
        p.start()
    try:
        assert frc.candidate_is_soonest_cooled(*args) is True
    finally:
        for p in patches:
            p.stop()


def test_reclaim_scan_range_excludes_the_active_and_later_entries():
    """P2-d: _try_reclaim_earlier_fallback scans [0, before_index), not from the live cursor."""
    now = time.time()
    arm_cooldown(ROUTE_ONE, reason=FailoverReason.billing, reset_at=now + 3600)
    arm_cooldown(ROUTE_TWO, reason=FailoverReason.billing, reset_at=now + 7200)
    agent = _agent([_entry(ROUTE_ONE), _entry(ROUTE_TWO), _entry(ROUTE_THREE)])
    agent._fallback_index = 3  # route three is bound and uncooled
    from agent import fallback_route_recovery as recovery
    with (
        patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None),
        patch("agent.fallback_route_cooldowns.candidate_pool_exhausted", return_value=False),
        patch.object(recovery, "_capture_route_state", return_value={}),
        patch.object(recovery, "_restore_route_state"),
        patch("agent.chat_completion_helpers._finalize_fallback_route"),
        patch("agent.route_binding.bind_route_entry", side_effect=[None, (agent.model, agent.provider)]) as bind,
    ):
        assert recovery._try_reclaim_earlier_fallback(agent, before_index=2) is True
    # The earliest cooled candidate could not bind. It must not block the next candidate,
    # nor should the uncooled current route outside the reclaim range enter the decision.
    assert [call.args[1]["model"] for call in bind.call_args_list] == [ROUTE_ONE[2], ROUTE_TWO[2]]
    assert agent._fallback_index == 2


def test_primary_recovery_spares_an_independent_fallback_billing_record(tmp_path, monkeypatch):
    """Primary recovery proves nothing about independently failed fallback routes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent.shared_primary_cooldown import clear_if_current

    agent = _agent([_entry(ROUTE_ONE), _entry(ROUTE_TWO)])
    agent._fallback_activated = False
    agent.provider, agent.base_url, agent.model = PRIMARY
    _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit)
    primary_record = agent._shared_primary_cooldown_record
    agent._fallback_activated = True
    agent.provider, agent.base_url, agent.model = ROUTE_ONE
    _arm_rate_limit_cooldown(agent, FailoverReason.rate_limit)
    agent.provider, agent.base_url, agent.model = ROUTE_TWO
    _arm_rate_limit_cooldown(agent, FailoverReason.billing)

    assert clear_if_current(PRIMARY, primary_record["outage_id"]) is True
    assert _record(PRIMARY) is None
    assert _record(ROUTE_ONE)["source"] == "backoff"
    assert _record(ROUTE_TWO)["source"] == "billing_default"
