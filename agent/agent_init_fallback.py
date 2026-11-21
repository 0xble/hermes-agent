"""Init-time fallback chain: entry normalization, chain setup, and the fallback's api_mode."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger("run_agent")


def _fallback_entries(fallback_model) -> list[dict[str, Any]]:
    """Normalize legacy single-dict ``fallback_model`` / list ``fallback_providers``."""
    if isinstance(fallback_model, dict):
        fallback_model = [fallback_model]
    if not isinstance(fallback_model, list):
        return []
    return [
        f for f in fallback_model if isinstance(f, dict) and f.get("provider") and f.get("model")
    ]


def _pin_init_fallbacks(agent, fallback_model, model_pinned, fallback_chain_owned=False):
    from hermes_cli.fallback_config import same_model_fallbacks

    allow_billing_fallback = bool(getattr(agent, "allow_billing_fallback", False))
    agent._pinned_fallback_model = agent.model if model_pinned and not fallback_chain_owned else None
    # A route owner may explicitly provide its own fallback chain. That chain is already scoped
    # by the owner, so an explicit model pin must not discard its cross-model entries during init.
    if model_pinned and not fallback_chain_owned and not allow_billing_fallback:
        return same_model_fallbacks(_fallback_entries(fallback_model), agent.model)
    return fallback_model


def _bind_init_fallback(agent, provider, model, client):
    """Bind a resolved init route; return (accepted, needs_client_kwargs)."""
    from agent.fallback_attribution import note_fallback
    from hermes_cli.fallback_config import same_fallback_model

    pinned = getattr(agent, "_pinned_fallback_model", None)
    if isinstance(pinned, str) and pinned and not same_fallback_model(pinned, model) and not getattr(agent, "_billing_fallback_attempt", False):
        return False, False
    previous_model = agent.model
    agent._fallback_activated = True
    is_moa = provider.strip().lower() == "moa"
    if is_moa:
        # Resolve checks the preset's aggregator credentials, but the agent uses the MoA facade.
        from agent.moa_loop import bind_moa_runtime
        bind_moa_runtime(agent, model)
    else:
        agent.provider = agent.requested_provider = provider
        agent.model = model
        recompute_init_fallback_api_mode(agent, client)
    note_fallback(agent, previous_model, agent.model, provider, "credentials_unavailable")
    # Preserve construction-time attribution across the first turn reset so the
    # result names the original requested model and credential failure.
    agent._preflight_fallback = dict(agent._fallback_attribution)
    return True, not is_moa


def _init_fallback_chain(agent, fallback_model):
    # Stable pool-entry identity: OAuth refreshes can replace the token before a failed
    # request is recovered, so the key value alone can't attribute the failure.
    from agent.agent_runtime_helpers import sync_credential_pool_entry_id
    sync_credential_pool_entry_id(agent)

    # Ordered backups tried when the primary is exhausted (legacy single-dict or list).
    agent._fallback_chain = _fallback_entries(fallback_model)
    agent._fallback_index = 0
    agent._fallback_activated = getattr(agent, "_fallback_activated", False)
    agent._fallback_episode_cooled = getattr(agent, "_fallback_episode_cooled", False)
    # Legacy attribute kept for backward compat (tests, external callers)
    agent._fallback_model = agent._fallback_chain[0] if agent._fallback_chain else None
    chain = agent._fallback_chain
    if chain and not agent.quiet_mode:
        labels = [f"{f['model']} ({f['provider']})" for f in chain]
        if len(chain) == 1:
            print(f"🔄 Fallback model: {labels[0]}")
        else:
            print(f"🔄 Fallback chain ({len(chain)} providers): " + " → ".join(labels))


def recompute_init_fallback_api_mode(agent, fb_client) -> None:
    """Give an init-time fallback its own api_mode instead of the unreachable primary's.

    A Nous primary configured with ``api_mode: codex_responses`` otherwise leaves a fallback whose
    profile serves that model on chat completions (``api_mode_for_model``) on the Responses API,
    which silently drops reasoning content (#46527).
    The per-turn path (``try_activate_fallback``) already recomputes via
    ``_fallback_api_mode_resolved``; this keeps init in agreement, and runs before the
    ``_primary_runtime`` snapshot so ``restore_primary_runtime`` replays the recomputed mode.
    A detection failure keeps the primary's mode and never blocks the fallback itself.
    """
    from agent.chat_completion_helpers import _fallback_api_mode_resolved
    try:
        agent.api_mode = _fallback_api_mode_resolved(
            agent, agent.provider, agent.model, str(getattr(fb_client, "base_url", "") or ""))
    except Exception:
        logger.debug("Init-time fallback api_mode detection failed for %s", agent.provider, exc_info=True)
        return
    if hasattr(agent, "_transport_cache"):
        agent._transport_cache.clear()
