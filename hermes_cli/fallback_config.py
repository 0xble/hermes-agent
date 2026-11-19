"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# Minimum chain policy is shared by config validation and web-route writes.
MIN_FALLBACK_ENTRIES = 3
MIN_UPSTREAM_FAMILIES = 3

# Model names are more reliable than a provider label when a local proxy multiplexes
# several vendors (for example muse-* is Meta behind custom:openai-proxy).
_MODEL_UPSTREAM_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude", "anthropic"),
    ("gpt", "openai"), ("o1", "openai"), ("o3", "openai"),
    ("o4", "openai"), ("o-series", "openai"), ("codex", "openai"),
    ("muse", "meta"), ("llama", "meta"),
    ("grok", "xai"), ("gemini", "google"), ("gemma", "google"),
    ("mistral", "mistral"), ("mixtral", "mistral"), ("deepseek", "deepseek"),
    ("qwen", "alibaba"), ("kimi", "moonshot"), ("minimax", "minimax"),
)
# First-party provider ids whose vendor is known even when the model name is not. Read before
# alias collapse: ``openai`` is also a legacy alias of an aggregator id.
_PROVIDER_UPSTREAM_FAMILIES = {
    "openai": "openai", "openai-codex": "openai", "anthropic": "anthropic",
    "xai": "xai", "xai-oauth": "xai", "meta": "meta",
}


def infer_upstream_family(provider: Any, model: Any, explicit: Any = None) -> str:
    """Upstream family of one concrete route: *explicit*, else the model name, else the provider.

    The single resolver behind config validation and the spare-route guards, so both judge a
    route the same way. A vendor segment is dropped before the model name is read
    (``anthropic/claude-x`` on an aggregator is Anthropic, not the aggregator). No alias is
    expanded here; :func:`upstream_family` does that first for config routes.
    """
    declared = str(explicit or "").strip().lower()
    if declared:
        return declared
    name = str(model or "").strip().lower().rsplit("/", 1)[-1]
    for prefix, family in _MODEL_UPSTREAM_PREFIXES:
        if name.startswith(prefix):
            return family
    provider_id = str(provider or "").strip().lower()
    if provider_id in _PROVIDER_UPSTREAM_FAMILIES:
        return _PROVIDER_UPSTREAM_FAMILIES[provider_id]
    # _normalize_provider_id already degrades to the bare lowercase id when it cannot load.
    return _normalize_provider_id(provider_id) or f"model:{name}"


def _resolved_route(route: Mapping[str, Any] | None, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a route copy with model aliases expanded for identity/family checks."""
    item = dict(route or {})
    model = str(item.get("model") or "").strip()
    if model:
        from hermes_cli.model_alias_runtime import resolve_model_alias
        try:
            resolved = resolve_model_alias(model, config or {})
        except Exception:
            # Config validation runs at every startup and must never fail on one route's alias.
            # An unresolvable alias is reported by validation as the malformed route it is;
            # identity and family checks fall back to the name as written.
            logger.debug("Fallback route alias %r did not resolve", model, exc_info=True)
            return item
        if resolved.name and resolved.model:
            item["model"] = resolved.model
            if not item.get("provider") and resolved.provider:
                item["provider"] = resolved.provider
            if not item.get("base_url") and resolved.base_url:
                item["base_url"] = resolved.base_url
    return item


def upstream_family(route: Mapping[str, Any] | None, config: Mapping[str, Any] | None = None) -> str:
    """Resolve the independent upstream family for a route.

    An explicit ``upstream`` wins. Otherwise aliases are expanded before model-name
    inference; provider labels are only the final fallback for unknown model names.
    """
    item = _resolved_route(route, config)
    return infer_upstream_family(item.get("provider"), item.get("model"), item.get("upstream"))


def route_identity(route: Mapping[str, Any] | None, config: Mapping[str, Any] | None = None) -> tuple[str, str, str]:
    """Canonical provider/model/base-url identity, after model-alias expansion."""
    item = _resolved_route(route, config)
    return (
        _normalize_provider_id(str(item.get("provider") or "").strip()).lower(),
        str(item.get("model") or "").strip().lower(),
        _normalized_base_url(item.get("base_url")).lower(),
    )


def _normalized_base_url(value: Any) -> str:
    return value.strip().rstrip("/") if isinstance(value, str) else ""


def _normalize_provider_id(value: str) -> str:
    """Collapse provider aliases to the canonical id (lazy to avoid import cycles).

    ``opencode-zen`` is the legacy pre-rename id of ``opencode``; ``zen`` is a
    short alias. Without normalization, two fallback entries naming the same
    backend under different ids are kept as separate slots, shifting the chain
    and letting an alias slip past the same-backend skip (#85235).
    """
    try:
        from hermes_cli.providers import normalize_provider
    except Exception:  # pragma: no cover - import resilience
        # Silent degradation: alias collapse is disabled and we fall back to a
        # bare lowercase. Log so a future import cycle/refactor can't regress
        # the dedup invisibly.
        logger.warning(
            "fallback provider alias collapse degraded to lowercase: "
            "could not import hermes_cli.providers.normalize_provider",
            exc_info=True,
        )
        return value.lower()
    return normalize_provider(value)


def resolve_entry_api_key(entry: dict[str, Any] | None) -> str | None:
    """API key for one fallback entry: inline ``api_key``, else ``key_env``.

    Mirrors the custom-provider convention (``api_key_env`` accepted as alias); None when neither
    yields a value so ``resolve_runtime_provider`` falls through to standard credential resolution.
    ``key_env`` goes through ``agent.secret_scope.get_secret``, not raw ``os.getenv``: in a
    multiplexed gateway a bare env read ignores the active profile's scope and can return another
    profile's credential.
    """
    if not isinstance(entry, dict):
        return None
    if inline := str(entry.get("api_key") or "").strip():
        return inline
    if key_env := str(entry.get("key_env") or entry.get("api_key_env") or "").strip():
        from agent.secret_scope import get_secret
        return (get_secret(key_env) or "").strip() or None
    return None


def effective_runtime_provider(
    entry: dict[str, Any] | None, runtime: dict[str, Any] | None
) -> str:
    """Provider identity to persist/display for a resolved fallback entry.

    ``resolve_runtime_provider`` returns the bare billing class ``"custom"``
    for every named ``providers:`` / ``custom_providers:`` entry; the entry's
    configured id only survives in ``requested_provider``. Fallback resolvers
    that persist ``runtime["provider"]`` as the agent identity therefore label
    sessions/billing rows ``custom`` instead of the configured provider name —
    while the manual ``/model`` switch path correctly persists the named id
    (#98739). Same class as the delegation fix in ``tools/delegate_tool.py``.

    Returns the entry's requested identity when the resolved provider is the
    bare ``custom`` class; a genuinely ad-hoc endpoint (requested provider IS
    ``custom``) keeps the bare class unchanged.
    """
    runtime = runtime or {}
    resolved = str(runtime.get("provider") or "").strip()
    if resolved.lower() != "custom":
        return resolved
    requested = str(
        runtime.get("requested_provider")
        or (entry or {}).get("provider")
        or ""
    ).strip()
    if requested and requested.lower() != "custom":
        return requested
    return resolved


def pre_agent_fallback_notice(
    primary_provider: Any, primary_model: Any, fallback_provider: Any, fallback_model: Any
) -> str:
    """User-visible one-shot line for a provider switch made during credential resolution, before
    any AIAgent exists (#74349). Shared by the messaging gateway, the TUI/Desktop gateway and cron
    so the three pre-agent fallback paths cannot drift in wording."""
    primary_desc = "/".join(str(p).strip() for p in (primary_provider, primary_model) if p) or "primary"
    fallback_desc = "/".join(str(p).strip() for p in (fallback_provider, fallback_model) if p) or "fallback"
    return f"⚠️ Provider fallback: {primary_desc} unavailable; using {fallback_desc} for this response."



def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    candidates = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    entries: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue
        normalized = {**entry, "provider": provider, "model": model}
        base_url = _normalized_base_url(entry.get("base_url"))
        if base_url:
            normalized["base_url"] = base_url
        entries.append(normalized)
    return entries


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _normalize_provider_id(str(entry.get("provider") or "").strip()),
        str(entry.get("model") or "").strip().lower(),
        _normalized_base_url(entry.get("base_url")).lower(),
    )


def same_fallback_model(requested: str, candidate: str) -> bool:
    """Exact model identity, allowing the conventional Anthropic Claude namespace.

    Don't infer equivalence from a vendor/family prefix, strip version or tier suffixes,
    or case-fold opaque custom model IDs.
    """
    requested, candidate = requested.strip(), candidate.strip()
    return bool(requested and candidate) and (
        requested == candidate
        or (requested.startswith("anthropic/claude-") and requested.removeprefix("anthropic/") == candidate)
        or (candidate.startswith("anthropic/claude-") and candidate.removeprefix("anthropic/") == requested)
    )


def same_model_fallbacks(chain: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    return [entry for entry in chain if same_fallback_model(model, str(entry.get("model") or ""))]


def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the effective fallback chain merged across old and new config keys.

    ``fallback_providers`` remains the primary source of truth and keeps its order. Legacy
    ``fallback_model`` entries are appended afterwards unless they target the same
    provider/model/base_url route as an earlier entry. The returned list always contains fresh dict
    copies.
    """
    # Raw effective config and already-expanded config share this boundary.
    # Resolve the complete route before concrete-entry parsing can discard refs.
    config = config or {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for key in ("fallback_providers", "fallback_model"):
        for entry in _iter_fallback_entries(config.get(key)):
            identity = _entry_identity(entry)
            if identity not in seen:
                seen.add(identity)
                chain.append(entry)
    return chain


def get_cron_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Resolve cron's override without changing the main agent's fallback policy.

    Missing/null inherits the global chain, including legacy fallback_model.
    An explicit value replaces it entirely; [] disables cron provider fallback.
    Invalid entries are dropped by the shared normalizer, never rescued globally.
    """
    config = config or {}
    cron = config.get("cron")
    declared = cron.get("fallback_providers") if isinstance(cron, dict) else None
    if declared is None:
        return get_fallback_chain(config)
    return get_fallback_chain({
        "fallback_providers": declared,
    })

def scoped_fallback_chain(
    inherited: list[dict[str, Any]] | None, declared: Any, *, pinned: bool, owner: str,
) -> list[dict[str, Any]] | None:
    """Fallback chain for a route owner that can pin its own primary (delegated child, cron job).

    A pinned owner (explicit provider, endpoint or model) never borrows the *inherited* chain: the
    operator chose that route, and a chain entry is a different provider and usually a different
    model. Predictability beats liveness for an explicit pin. An unpinned owner inherits the chain
    when *declared* is absent/None. An explicit ``[]`` disables fallback either way; any other
    *declared* value is the owner's own chain, normalized by :func:`get_fallback_chain` (malformed
    entries are dropped; nothing usable left falls back to the pinned/inherited default).
    """
    default = None if pinned else (inherited or None)
    if declared is None:
        return default
    if declared == []:
        return None
    normalized = get_fallback_chain({"fallback_providers": declared})
    if not normalized:
        logger.warning("%s fallback_providers has no usable routes; using the %s default",
                       owner, "pinned" if pinned else "inherited")
    return normalized or default
