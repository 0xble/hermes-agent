"""A session pin declining a fallback does not mean its provider is unavailable."""

import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agent.chat_completion_helpers import _fallback_entry_key, try_activate_fallback
from agent.error_classifier import FailoverReason

PINNED = "gpt-6.2-astra"
PROVIDER = "custom:backup"
LOGGER = "agent.chat_completion_helpers"


def _agent(model: str, *, pinned: str | None = PINNED):
    return SimpleNamespace(
        provider="custom:primary", model=PINNED, client=Mock(),
        base_url="https://primary.example.com/v1", api_mode="chat_completions",
        _fallback_activated=False, _fallback_index=0,
        _fallback_chain=[{"provider": PROVIDER, "model": model, "api_mode": "chat_completions"}],
        _pinned_fallback_model=pinned,
        _unavailable_fallback_keys={("known-unavailable", "other-model", "")},
    )


def _try(agent, *, client=None, reason=None):
    with (
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, None)) as resolve,
        patch("agent.chat_completion_helpers._should_skip_fallback_candidate", return_value=False),
        patch("agent.fallback_cooldown._arm_rate_limit_cooldown"),
        patch("agent.spare_guards.reserve_fallback_route", return_value=object()),
        patch("agent.spare_guards.release_fallback_route") as release,
    ):
        assert try_activate_fallback(agent, reason) is False
    release.assert_called_once()
    return resolve


@pytest.mark.parametrize("normalize", [False, True], ids=["before-lookup", "after-normalization"])
def test_pin_rejection_logs_policy_and_leaves_route_available(caplog, normalize):
    agent = _agent(PINNED if normalize else "different-model")
    before = set(agent._unavailable_fallback_keys)
    primary_client = agent.client
    candidate = Mock(base_url="https://candidate.example.com/v1")

    caplog.set_level(logging.INFO, logger=LOGGER)
    with patch("hermes_cli.model_normalize.normalize_model_for_provider", return_value="different-model"):
        resolve = _try(agent, client=candidate, reason=FailoverReason.server_error)

    if normalize:
        resolve.assert_called_once()
        candidate.close.assert_called_once()
    else:
        resolve.assert_not_called()
        candidate.close.assert_not_called()
    assert agent.client is primary_client
    assert agent.model == PINNED
    assert not agent._fallback_activated
    assert "provider not configured" not in caplog.text
    assert (LOGGER, logging.INFO, f"Fallback to {PROVIDER} skipped: session pinned to {PINNED}") in caplog.record_tuples
    assert agent._unavailable_fallback_keys == before


@pytest.mark.parametrize(
    "pinned, model, reason",
    [
        (None, "different-model", FailoverReason.server_error),
        (PINNED, PINNED, FailoverReason.server_error),
        (PINNED, "different-model", FailoverReason.billing),
    ],
    ids=["unpinned", "same-model", "billing-exception"],
)
def test_unconfigured_provider_still_warns(caplog, pinned, model, reason):
    agent = _agent(model, pinned=pinned)
    before = set(agent._unavailable_fallback_keys)
    caplog.set_level(logging.INFO, logger=LOGGER)

    _try(agent, reason=reason).assert_called_once()

    assert (LOGGER, logging.WARNING, f"Fallback to {PROVIDER} failed: provider not configured") in caplog.record_tuples
    assert "session pinned" not in caplog.text
    assert agent._unavailable_fallback_keys == before | {_fallback_entry_key(agent._fallback_chain[0])}
