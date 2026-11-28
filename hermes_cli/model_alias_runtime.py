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
    if value is None or value is True or value == "" or value == {}:
        return ""
    parsed = parse_reasoning_effort(value)
    if parsed is None:
        raise ModelAliasError(f"Unknown reasoning effort {value!r}")
    if not parsed["enabled"]:
        return "none"
    return parsed if isinstance(value, dict) else parsed["effort"]


def alias_name(value: Any, config: Mapping[str, Any] | None = None) -> str | None:
    """Only upstream's explicitly authored labels are references, never @ syntax."""
    if not isinstance(value, str) or not value.strip():
        return None
    from hermes_cli.model_switch import _load_direct_aliases
    key = value.strip().lower()
    return key if key in _load_direct_aliases(dict(config or {})) else None


def alias_names(config: Mapping[str, Any] | None) -> list[str]:
    """The aliases ``/model`` resolves (``model_aliases`` plus ``model.aliases``), from the same loader."""
    from hermes_cli.model_switch import _load_direct_aliases
    return sorted(_load_direct_aliases(dict(config or {})))


def unknown_alias_message(value: Any, names: list[str]) -> str:
    known = f"Use one of: {', '.join(names)}" if names else "No model aliases are configured"
    return f"unknown model alias {value!r}. {known} (model_aliases in config.yaml)."


def card_model_alias(value: str, config: Mapping[str, Any] | None = None) -> str | None:
    """The alias key a Kanban card's model pin names, or None for a raw model id.

    A configured alias name (case-insensitive) is an alias; a leading ``@`` marks the value as an
    alias, so an unconfigured ``@name`` is refused instead of being sent as a model id.
    """
    if config is None:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
    names = alias_names(config)
    key = value.strip().lower()
    if key.removeprefix("@") in names:
        return key.removeprefix("@")
    if key.startswith("@"):
        raise ModelAliasError(unknown_alias_message(value, names))
    return None


def card_launch_route(model: str | None, provider: str | None, reasoning: str | None,
                      config: Mapping[str, Any]) -> dict[str, str]:
    """Session create fields for a card's pin, resolved from the owner's current config.

    A card pinned to an alias (no ``provider_override``) runs the alias's live model, provider,
    endpoint and default reasoning effort; the card's own effort wins. A raw id passes through.
    """
    fields = {"model": model, "provider": provider, "reasoning": reasoning}
    if model and not provider and alias_name(model, config):
        route = resolve_model_alias(model, config, required=True)
        fields.update(model=route.model, provider=route.provider, base_url=route.base_url)
        if not reasoning and isinstance(route.reasoning_effort, str):
            fields["reasoning"] = route.reasoning_effort
    return {name: value for name, value in fields.items() if value}


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
    return AliasRoute(provider=str(model.get("provider") or ""), model=route.model,
                      reasoning_effort=normalize_route_reasoning_effort(model.get("reasoning_effort")),
                      base_url=str(model.get("base_url") or ""))
