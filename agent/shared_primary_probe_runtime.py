"""Runtime bridge for the shared primary cooldown's half-open probe."""

import contextlib
import logging

logger = logging.getLogger(__name__)


def _configured_base_url(provider, model) -> str:
    """Find a candidate endpoint from config only, without auth or provider resolution."""
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
    except (ImportError, OSError, TypeError, ValueError, KeyError):
        return ""
    wanted = str(provider or "").strip().lower()
    target_model = str(model or "").strip()
    model_cfg = config.get("model") if isinstance(config, dict) else None
    if isinstance(model_cfg, dict):
        configured_provider = str(model_cfg.get("provider") or "").strip().lower()
        configured_model = str(model_cfg.get("model") or "").strip()
        if (configured_provider == wanted and configured_model == target_model
                and model_cfg.get("base_url")):
            return str(model_cfg["base_url"]).strip()
    providers = config.get("providers") if isinstance(config, dict) else None
    if isinstance(providers, dict):
        for key, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            key_name = str(key).strip().lower()
            display_name = str(entry.get("name") or "").strip().lower()
            names = {key_name, display_name, f"custom:{key_name}", f"custom:{display_name}"}
            if wanted in names:
                return str(entry.get("api") or entry.get("url") or entry.get("base_url") or "").strip()
    for section in ("custom_providers", "fallback_providers"):
        entries = config.get(section) if isinstance(config, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_provider = str(entry.get("provider") or entry.get("name") or "").strip().lower()
            entry_model = str(entry.get("model") or "").strip()
            if entry_provider == wanted and (not target_model or not entry_model or entry_model == target_model):
                return str(entry.get("api") or entry.get("url") or entry.get("base_url") or "").strip()
    return ""


def _builtin_base_url(provider: str) -> str:
    """Return a built-in endpoint without resolving credentials or invoking recovery."""
    normalized = str(provider or "").strip().lower()
    if normalized == "openrouter":
        from hermes_constants import OPENROUTER_BASE_URL
        return OPENROUTER_BASE_URL
    try:
        from hermes_cli.providers import get_provider
        definition = get_provider(normalized, allow_network=False)
        return str(getattr(definition, "base_url", "") or "").strip()
    except (ImportError, OSError, TypeError, ValueError, KeyError):
        return ""


def fallback_route_is_active(provider, base_url, model) -> bool:
    from agent.shared_primary_cooldown import active_cooldown, canonical_route
    resolved_base_url = (
        str(base_url or "").strip()
        or _configured_base_url(provider, model)
        or _builtin_base_url(provider)
    )
    return bool(active_cooldown(canonical_route((provider, resolved_base_url, model))))


def claim_primary_probe(agent, shared_state: str):
    if shared_state != "expired" or not isinstance(getattr(agent, "_shared_primary_cooldown_record", None), dict):
        return None
    from agent.shared_primary_cooldown import claim_probe, route_from_record
    record = agent._shared_primary_cooldown_record
    token = claim_probe(route_from_record(record), str(record.get("outage_id") or ""))
    if token is False:
        logger.info("Primary cooldown probe already owned elsewhere; staying on fallback")
        return False
    if token is None:
        agent._shared_primary_cooldown_record = None
        agent._shared_primary_probe_token = None
        return None
    agent._shared_primary_probe_token = token
    return token


def release_primary_probe(agent, token) -> None:
    if not token:
        return
    try:
        from agent.shared_primary_cooldown import _release_probe, route_from_record
        record = agent._shared_primary_cooldown_record
        _release_probe(route_from_record(record), str(record.get("outage_id") or ""), token)
    except Exception:
        logger.debug("Could not release primary cooldown probe", exc_info=True)
    agent._shared_primary_probe_token = None
