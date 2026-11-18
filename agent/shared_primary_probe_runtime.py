"""Runtime bridge for the shared primary cooldown's half-open probe."""

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


def primary_probe_lease_seconds(agent) -> float:
    """Size the lease to the primary request timeout plus the normal 120 s safety margin."""
    from hermes_cli.timeouts import get_provider_request_timeout
    from agent.shared_primary_cooldown import _PROBE_LEASE_SECONDS, _finite_float, route_from_agent
    provider, _, model = route_from_agent(agent)
    timeout = _finite_float(get_provider_request_timeout(provider, model))
    runtime = getattr(agent, "_primary_runtime", None)
    kwargs = runtime.get("client_kwargs", {}) if isinstance(runtime, dict) else getattr(agent, "_client_kwargs", {})
    raw = (kwargs or {}).get("timeout")
    client_timeout = _finite_float(raw if isinstance(raw, (int, float)) else getattr(raw, "read", None))
    # The OpenAI/Anthropic SDKs default to 600 s when config does not pin a timeout.
    return max(timeout or client_timeout or 600.0, 0.0) + _PROBE_LEASE_SECONDS


def claim_primary_probe(agent, shared_state: str):
    from agent.shared_primary_cooldown import ProbeClaimKind, ProbeClaimOutcome
    if shared_state != "expired" or not isinstance(getattr(agent, "_shared_primary_cooldown_record", None), dict):
        from agent.shared_primary_cooldown import primary_probe_is_owned
        primary_probe_is_owned(agent)
        return ProbeClaimOutcome(ProbeClaimKind.CLEARED)
    from agent.shared_primary_cooldown import claim_probe, route_from_record
    record = agent._shared_primary_cooldown_record
    try:
        outcome = claim_probe(
            route_from_record(record), str(record.get("outage_id") or ""),
            lease_seconds=primary_probe_lease_seconds(agent),
        )
    except OSError:
        logger.warning("Primary cooldown probe state is unavailable; staying on fallback")
        outcome = ProbeClaimOutcome(ProbeClaimKind.UNAVAILABLE)
    if not isinstance(outcome, ProbeClaimOutcome):
        logger.warning("Primary cooldown probe returned an invalid outcome; staying on fallback")
        outcome = ProbeClaimOutcome(ProbeClaimKind.UNAVAILABLE)
    if outcome.kind == ProbeClaimKind.CLAIMED:
        agent._shared_primary_probe_token = outcome.token
        return outcome
    if outcome.kind in (ProbeClaimKind.OWNED_ELSEWHERE, ProbeClaimKind.STILL_COOLING,
                        ProbeClaimKind.REPLACED, ProbeClaimKind.UNAVAILABLE):
        if outcome.kind == ProbeClaimKind.REPLACED and isinstance(outcome.record, dict):
            agent._shared_primary_cooldown_record = outcome.record
        logger.info("Primary cooldown probe unavailable (%s); staying on fallback", outcome.kind.value)
        agent._shared_primary_probe_token = None
        return outcome
    if outcome.kind == ProbeClaimKind.CLEARED:
        agent._shared_primary_cooldown_record = None
        agent._shared_primary_probe_token = None
        return outcome
    raise AssertionError(f"unhandled probe claim outcome: {outcome.kind!r}")


def probe_claim_allows_primary(outcome) -> bool:
    from agent.shared_primary_cooldown import ProbeClaimKind
    return outcome.kind in (ProbeClaimKind.CLAIMED, ProbeClaimKind.CLEARED)


def release_primary_probe(agent, token) -> None:
    if not token:
        return
    try:
        from agent.shared_primary_cooldown import settle_primary_probe_on_exit
        settle_primary_probe_on_exit(agent, reason="release")
    except Exception:
        logger.debug("Could not release primary cooldown probe", exc_info=True)
    agent._shared_primary_probe_token = None
