"""Config-driven, exact-origin aliases for browser-vault login metadata."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Dict, Iterable
from urllib.parse import urlsplit

from agent.vault_store import VaultError, VaultItemMeta, normalize_origin

logger = logging.getLogger(__name__)


def _normalize_alias_origin(value: Any) -> str | None:
    """Return one exact HTTPS origin, or None for malformed/unsafe config."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or "*" in raw:
        return None
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme.lower() != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            return None
        return normalize_origin(raw)
    except (TypeError, ValueError, VaultError):
        return None


def _configured_aliases() -> Dict[str, tuple[str, ...]]:
    """Read and validate ``vault.origin_aliases`` from the active profile config."""
    from hermes_cli.config import load_config_readonly

    try:
        config = load_config_readonly() or {}
    except Exception as exc:  # A malformed config must not widen an origin binding.
        logger.warning("Ignoring vault.origin_aliases because config could not be read: %s", exc)
        return {}
    vault_config = config.get("vault") if isinstance(config, dict) else None
    raw_aliases = vault_config.get("origin_aliases") if isinstance(vault_config, dict) else None
    if not isinstance(raw_aliases, dict):
        return {}

    aliases: Dict[str, tuple[str, ...]] = {}
    for raw_key, raw_origins in raw_aliases.items():
        key = raw_key.strip() if isinstance(raw_key, str) else ""
        if not key:
            continue
        values: Iterable[Any] = [raw_origins] if isinstance(raw_origins, str) else raw_origins
        if not isinstance(values, (list, tuple)):
            continue
        normalized: list[str] = []
        for value in values:
            origin = _normalize_alias_origin(value)
            if origin and origin not in normalized:
                normalized.append(origin)
        if normalized:
            aliases[key] = tuple(normalized)
    return aliases


def _alias_keys(meta: VaultItemMeta) -> tuple[str, ...]:
    """Return exact handle keys plus raw IDs supported by 1Password handles."""
    keys = [meta.id]
    # Raw item IDs are accepted only for 1Password handles. This avoids a raw ID
    # collision from applying an alias to a local or Bitwarden item.
    if meta.id.startswith(("op:", "op@")):
        raw_id = meta.id.rsplit(":", 1)[-1]
        if raw_id and raw_id not in keys:
            keys.append(raw_id)
    return tuple(keys)


def _meta_with_origin_aliases(meta: VaultItemMeta) -> VaultItemMeta:
    """Augment one login's exact allowed origins without changing its vault item."""
    if meta.kind != "login" or not meta.origin:
        return meta
    configured = _configured_aliases()
    extra: list[str] = []
    for key in _alias_keys(meta):
        for origin in configured.get(key, ()):
            if origin not in extra:
                extra.append(origin)
    if not extra:
        return meta
    existing = list(meta.allowed_origins) or [meta.origin]
    allowed = tuple(existing + [origin for origin in extra if origin not in existing])
    return replace(meta, allowed_origins=allowed)


def apply_origin_aliases(metas: Iterable[VaultItemMeta]) -> list[VaultItemMeta]:
    """Apply the shared registry path to metadata returned by a backend."""
    return [_meta_with_origin_aliases(meta) for meta in metas]
