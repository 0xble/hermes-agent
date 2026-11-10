"""Live references to upstream model_aliases at persisted route boundaries."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


class ModelAliasError(ValueError):
    """A stored alias no longer identifies a usable route."""


@dataclass(frozen=True)
class AliasRoute:
    name: str = ""
    provider: str = ""
    model: str = ""
    reasoning_effort: Any = ""
    base_url: str = ""
    api_key: str = ""


def normalize_route_reasoning_effort(value: Any) -> Any:
    from hermes_constants import parse_reasoning_effort
    if value in (None, ""):
        return ""
    parsed = parse_reasoning_effort(value)
    if parsed is None:
        raise ModelAliasError(f"Unknown reasoning effort {value!r}")
    return value if isinstance(value, (str, dict)) else parsed


def alias_name(value: Any, config: Mapping[str, Any] | None = None) -> str | None:
    """Only upstream's explicitly authored labels are references, never @ syntax."""
    if not isinstance(value, str) or not value.strip():
        return None
    from hermes_cli.model_switch import _load_direct_aliases
    key = value.strip().lower()
    return key if key in _load_direct_aliases(dict(config or {})) else None


def resolve_model_alias(value: Any, config: Mapping[str, Any] | None = None, *, required: bool = False) -> AliasRoute:
    """Resolve from the request's current config snapshot without expanding saved config."""
    cfg = dict(config or {})
    key = str(value or "").strip().lower()
    from hermes_cli.model_switch import _load_direct_aliases
    alias = _load_direct_aliases(cfg).get(key)
    if alias is None:
        if required or re.fullmatch(r"@[A-Za-z0-9_-]+", key):
            raise ModelAliasError(f"Unknown model alias {value!r}. Define it in model_aliases.")
        model_cfg = cfg.get("model")
        provider = str(model_cfg.get("provider") or "") if isinstance(model_cfg, Mapping) else ""
        return AliasRoute(provider=provider, model=str(value or "").strip())
    entry = next((v for k, v in (cfg.get("model_aliases") or {}).items()
                  if str(k).strip().lower() == key), {})
    from hermes_cli.model_switch import resolve_startup_model_route
    resolved = resolve_startup_model_route(key, alias_config=cfg)
    return AliasRoute(key, resolved.provider, resolved.model,
                      normalize_route_reasoning_effort(entry.get("reasoning_effort")), resolved.base_url,
                      api_key=resolved.api_key)


def resolve_default_route(config: Mapping[str, Any] | None) -> AliasRoute:
    cfg = dict(config or {})
    model = cfg.get("model")
    if isinstance(model, str):
        return resolve_model_alias(model, cfg)
    if not isinstance(model, Mapping):
        return AliasRoute()
    raw = model.get("default") or model.get("model") or ""
    route = resolve_model_alias(raw, cfg)
    if route.name:
        return route
    return AliasRoute(provider=str(model.get("provider") or ""), model=str(raw),
                      reasoning_effort=normalize_route_reasoning_effort(model.get("reasoning_effort")),
                      base_url=str(model.get("base_url") or ""))
