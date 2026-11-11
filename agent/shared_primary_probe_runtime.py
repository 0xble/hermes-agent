"""Runtime bridge for the shared primary cooldown's half-open probe."""

import contextlib
import logging

logger = logging.getLogger(__name__)


def fallback_route_is_active(provider, base_url, model) -> bool:
    from agent.shared_primary_cooldown import active_cooldown, canonical_route
    resolved_base_url = base_url or ""
    if not resolved_base_url:
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider
            runtime = resolve_runtime_provider(requested=provider, target_model=model)
            resolved_base_url = str(runtime.get("base_url") or "")
        except Exception:
            logger.debug("Could not resolve fallback route for cooldown lookup", exc_info=True)
    return bool(active_cooldown(canonical_route((provider, resolved_base_url, model))))


def claim_primary_probe(agent, shared_state: str):
    if shared_state != "expired" or not isinstance(getattr(agent, "_shared_primary_cooldown_record", None), dict):
        return None
    from agent.shared_primary_cooldown import claim_probe, route_from_record
    record = agent._shared_primary_cooldown_record
    token = claim_probe(route_from_record(record), str(record.get("outage_id") or ""))
    if not token:
        logger.info("Primary cooldown probe already owned elsewhere; staying on fallback")
        return False
    agent._shared_primary_probe_token = token
    return token


def settle_failed_primary_probe(agent, token) -> None:
    if not token:
        return
    with contextlib.suppress(Exception):
        from agent.shared_primary_cooldown import _settle_probe, route_from_record
        record = agent._shared_primary_cooldown_record
        _settle_probe(route_from_record(record), str(record.get("outage_id") or ""), token, success=False)
    agent._shared_primary_probe_token = None


def release_primary_probe(agent, token) -> None:
    if not token:
        return
    with contextlib.suppress(Exception):
        from agent.shared_primary_cooldown import _release_probe, route_from_record
        record = agent._shared_primary_cooldown_record
        _release_probe(route_from_record(record), str(record.get("outage_id") or ""), token)
    agent._shared_primary_probe_token = None
