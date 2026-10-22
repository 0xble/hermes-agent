from __future__ import annotations

import dataclasses
from typing import Any, Mapping

from hermes_cli.model_presets import (
    ModelPresetError, ModelPresetRoute, ReasoningEffort, normalize_route_reasoning_effort, preset_ref_name,
)


def _route_effort(site: Mapping[str, Any], inherited: ReasoningEffort = "") -> ReasoningEffort:
    return normalize_route_reasoning_effort(site.get("reasoning_effort")) or inherited


def resolve_model_ref(raw: Any, config: Mapping[str, Any] | None = None, *, _stack: tuple[str, ...] = ()) -> ModelPresetRoute:
    """Resolve a live model reference without mutating config.

    ``raw`` accepts a concrete model string, ``@name``, or the canonical
    ``{"model_preset": name}`` route shape. Presets are intentionally looked up
    on every call so editing a preset affects the next API request.
    """
    cfg = dict(config or {})
    model_value = cfg.get("model")
    model_cfg: Mapping[str, Any] = model_value if isinstance(model_value, Mapping) else {}
    from hermes_cli.model_presets import _fallbacks_disabled, _lift_main_model
    site = _lift_main_model(raw)
    disabled_fallbacks = _fallbacks_disabled(site, "model reference")
    raw = site
    explicit_name: str | None = None
    if isinstance(raw, Mapping):
        if "model_preset" in raw:
            explicit_name = str(raw.get("model_preset") or "").strip()
            if not explicit_name:
                raise ModelPresetError("model reference '@' is empty")
        else:
            raw = raw.get("model") or raw.get("default") or ""
    value = str(raw or "").strip()
    name = explicit_name if explicit_name is not None else preset_ref_name(value)
    if name is None:
        return ModelPresetRoute("", str(model_cfg.get("provider") or ""), value)
    if name in _stack:
        raise ModelPresetError("model reference cycle: " + " -> ".join((*_stack, name)))
    if name == "default":
        if model_cfg.get("model_preset") or preset_ref_name(model_cfg.get("default")) or preset_ref_name(model_cfg.get("model")):
            raw_model = model_cfg
        else:
            raw_model = model_value if isinstance(model_value, str) else (
                model_cfg.get("default") or model_cfg.get("model") or "")
        if isinstance(raw_model, Mapping) or preset_ref_name(raw_model):
            route = resolve_model_ref(raw_model, cfg, _stack=(*_stack, name))
            nested_provider = str(raw_model.get("provider") or "") if isinstance(raw_model, Mapping) else ""
            outer_provider = str(model_cfg.get("provider") or "")
            return ModelPresetRoute(name, str(nested_provider or outer_provider or route.provider), route.model,
                                    _route_effort(model_cfg, route.reasoning_effort), () if disabled_fallbacks else route.fallbacks)
        from hermes_cli.fallback_config import get_fallback_chain
        return ModelPresetRoute(name, str(model_cfg.get("provider") or ""), str(raw_model).strip(),
                                _route_effort(model_cfg), () if disabled_fallbacks else tuple(get_fallback_chain(cfg) or ()))
    if name == "attended":
        attended_value = model_cfg.get("attended")
        attended: Mapping[str, Any] = attended_value if isinstance(attended_value, Mapping) else {}
        raw_model = attended if attended.get("model_preset") or preset_ref_name(attended.get("model")) else attended.get("model", "")
        if isinstance(raw_model, Mapping) or preset_ref_name(raw_model):
            route = resolve_model_ref(raw_model, cfg, _stack=(*_stack, name))
            return ModelPresetRoute(name, str(attended.get("provider") or route.provider), route.model,
                                    _route_effort(attended, route.reasoning_effort), () if disabled_fallbacks else route.fallbacks)
        return ModelPresetRoute(name, str(attended.get("provider") or ""), str(raw_model), _route_effort(attended))
    from hermes_cli.model_presets import _definitions
    entry = _definitions(cfg).get(name)
    if entry is None:
        raise ModelPresetError(f"model_presets: unknown preset '{name}'")
    return ModelPresetRoute(
        name, entry["provider"], entry["model"], entry.get("reasoning_effort", ""),
        () if disabled_fallbacks else tuple(entry.get("fallbacks") or ()),
    )


def validate_model_refs(config: Mapping[str, Any] | None) -> list[str]:
    """Return actionable errors for every declared model_preset/@ reference."""
    cfg = dict(config or {})
    errors: list[str] = []
    model_value = cfg.get("model")
    model_cfg: Mapping[str, Any] = model_value if isinstance(model_value, Mapping) else {}
    candidates: list[tuple[str, Any]] = [("model.default", model_cfg.get("default")), ("model.attended.model", (model_cfg.get("attended") or {}).get("model") if isinstance(model_cfg.get("attended"), dict) else None)]
    presets = cfg.get("model_presets")
    if isinstance(presets, dict):
        candidates.extend((f"model_presets.{name}.model", entry.get("model")) for name, entry in presets.items() if isinstance(entry, dict))
    for path, raw in candidates:
        if preset_ref_name(raw):
            try:
                resolve_model_ref(raw, cfg)
            except ModelPresetError as exc:
                errors.append(f"{path}: {exc}")
    return errors


@dataclasses.dataclass(frozen=True)
class DefaultRoute:
    model: str = ""
    provider: str = ""
    reasoning_effort: ReasoningEffort = ""
    base_url: str = ""
    name: str = ""


def resolve_default_route(config: Mapping[str, Any] | None) -> DefaultRoute:
    """The concrete default route of a (possibly unexpanded) config.

    Single reader for ``model`` as a string, ``@name``, ``{default|model: "@name"}``,
    ``{model_preset: name}`` or plain concrete values, so no caller reads the raw
    ``model.default`` and sees an empty or stale route when a preset owns it. Raises
    ``ModelPresetError`` for an unknown or cyclic preset; fail-open callers catch it.
    """
    cfg = dict(config) if isinstance(config, Mapping) else {}
    raw = cfg.get("model")
    if not isinstance(raw, (str, Mapping)):
        return DefaultRoute()
    if isinstance(raw, str):
        route = resolve_model_ref(raw, cfg)
        return DefaultRoute(route.model, route.provider, route.reasoning_effort, name=route.name)
    route = resolve_model_ref("@default", cfg)
    return DefaultRoute(
        route.model, route.provider, route.reasoning_effort, str(raw.get("base_url") or ""), route.name,
    )
