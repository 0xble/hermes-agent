"""Shared cooldown lookup for fallback-chain candidates."""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("agent.chat_completion_helpers")


def _matching_entries(entries: list[dict], provider: str, model: str, base_url: str = "") -> list[dict]:
    from hermes_cli.model_normalize import normalize_model_for_provider
    from hermes_cli.route_identity import normalize_route_base_url
    normalized_provider = str(provider or "").strip().lower()
    wanted_model = normalize_model_for_provider(str(model or "").strip(), normalized_provider)
    wanted_base = normalize_route_base_url(base_url) if base_url else ""
    return [
        entry for entry in entries
        if str(entry.get("provider") or "").strip().lower() == normalized_provider
        and normalize_model_for_provider(str(entry.get("model") or "").strip(), normalized_provider) == wanted_model
        and (not wanted_base or normalize_route_base_url(entry.get("base_url")) == wanted_base)
    ]


def cooldown_entries(provider: str, model: str, base_url: str = "") -> list[dict]:
    """Return active durable cooldowns matching a fallback's provider/model route."""
    try:
        from agent.shared_primary_cooldown import list_cooldowns
        return _matching_entries(list_cooldowns(), provider, model, base_url)
    except Exception:
        logger.debug("Fallback cooldown read failed", exc_info=True)
        return []


def candidate_is_soonest_cooled(
    agent, provider: str, model: str, base_url: str, *, start: int | None = None,
    stop: int | None = None,
) -> bool:
    """Allow the soonest-reset usable route when every remaining fallback is cooled."""
    try:
        from agent.shared_primary_cooldown import list_cooldowns
        records = list_cooldowns()
    except Exception:
        logger.debug("Fallback cooldown read failed", exc_info=True)
        return False
    current = _matching_entries(records, provider, model, base_url)
    if not current:
        return False
    remaining = []
    chain = getattr(agent, "_fallback_chain", None) or []
    start = max(getattr(agent, "_fallback_index", 0) - 1, 0) if start is None else max(start, 0)
    stop = len(chain) if stop is None else min(stop, len(chain))
    unavailable = getattr(agent, "_unavailable_fallback_keys", None) or set()
    from agent.chat_completion_helpers import _fallback_entry_key, _fallback_entry_unavailable_without_network
    from agent.fallback_cooldown import _is_entitlement_rejected
    from agent.backend_identity import BackendIdentity, should_skip_candidate
    current_ident = BackendIdentity.build(provider=getattr(agent, "provider", ""),
        model=getattr(agent, "model", ""), base_url=str(getattr(agent, "base_url", "") or ""))
    for candidate in chain[start:stop]:
        candidate_provider = str(candidate.get("provider") or "").strip().lower()
        candidate_model = str(candidate.get("model") or "").strip()
        candidate_key = _fallback_entry_key(candidate)
        if (
            not candidate_provider or not candidate_model
            or candidate_key in unavailable
            or _is_entitlement_rejected(agent, candidate_provider, candidate_model)
            or candidate_pool_exhausted(agent, candidate_provider, candidate_model)
            or _fallback_entry_unavailable_without_network(agent, candidate)
        ):
            continue
        candidate_ident = BackendIdentity.build(provider=candidate_provider, model=candidate_model,
            base_url=candidate.get("base_url") or "")
        if should_skip_candidate(candidate_ident, current_ident) and candidate_provider == current_ident.provider:
            continue
        entries = _matching_entries(records, candidate_provider, candidate_model, candidate.get("base_url") or "")
        if not entries:
            return False
        remaining.append(entries)
    if not remaining:
        return False
    soonest = min(float(entry.get("reset_at", 0)) for entries in remaining for entry in entries)
    return min(float(entry.get("reset_at", 0)) for entry in current) <= soonest


def candidate_pool_exhausted(agent, fb_provider: str, fb_model: str) -> bool:
    """True when every credential for a candidate sits in a long exhaustion cooldown."""
    pool = getattr(agent, "_credential_pool", None)
    if pool is None or (getattr(pool, "provider", "") or "").strip().lower() != fb_provider:
        try:
            from agent.credential_pool import load_pool
            pool = load_pool(fb_provider)
        except Exception:
            logger.debug("Fallback credential pool load failed", exc_info=True)
            return False
    if pool is None or not pool.has_credentials() or pool.has_available(model=fb_model):
        return False
    until = pool.next_available_at(model=fb_model)
    return until is None or until - time.time() > 600
