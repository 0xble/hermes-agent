"""Shared cooldown lookup for fallback-chain candidates."""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("agent.chat_completion_helpers")


def cooldown_entries(provider: str, model: str, base_url: str = "") -> list[dict]:
    """Return active durable cooldowns matching a fallback's provider/model route."""
    try:
        from agent.shared_primary_cooldown import list_cooldowns
        from hermes_cli.route_identity import normalize_route_base_url
        wanted_base = normalize_route_base_url(base_url) if base_url else ""
        return [
            entry for entry in list_cooldowns()
            if str(entry.get("provider") or "").strip().lower() == provider
            and str(entry.get("model") or "").strip() == model
            and (not wanted_base or normalize_route_base_url(entry.get("base_url")) == wanted_base)
        ]
    except Exception:
        logger.debug("Fallback cooldown read failed", exc_info=True)
        return []


def candidate_is_soonest_cooled(agent, provider: str, model: str, base_url: str) -> bool:
    """Allow the soonest-reset route when every remaining fallback is cooled."""
    current = cooldown_entries(provider, model, base_url)
    if not current:
        return False
    remaining = []
    start = max(getattr(agent, "_fallback_index", 0) - 1, 0)
    for candidate in (getattr(agent, "_fallback_chain", None) or [])[start:]:
        candidate_provider = str(candidate.get("provider") or "").strip().lower()
        candidate_model = str(candidate.get("model") or "").strip()
        if candidate_provider and candidate_model:
            remaining.append(cooldown_entries(
                candidate_provider, candidate_model, candidate.get("base_url") or ""))
    if not remaining or any(not entries for entries in remaining):
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
