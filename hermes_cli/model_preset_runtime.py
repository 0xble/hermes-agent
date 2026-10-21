from __future__ import annotations

from typing import Any, Mapping

from hermes_cli.model_presets import ModelPresetError, ModelPresetRoute


def resolve_model_ref(raw: Any, config: Mapping[str, Any] | None = None, *, _stack: tuple[str, ...] = ()) -> ModelPresetRoute:
    """Resolve a live model reference without mutating config.

    ``raw`` accepts a concrete model string, ``@name``, or the canonical
    ``{"model_preset": name}`` route shape. Presets are intentionally looked up
    on every call so editing a preset affects the next API request.
    """
    cfg = dict(config or {})
    model_value = cfg.get("model")
    model_cfg: Mapping[str, Any] = model_value if isinstance(model_value, Mapping) else {}
    if isinstance(raw, Mapping):
        if "model_preset" in raw:
            raw = "@" + str(raw.get("model_preset") or "")
        else:
            raw = raw.get("model") or raw.get("default") or ""
    value = str(raw or "").strip()
    if not value.startswith("@"):
        return ModelPresetRoute("", str(model_cfg.get("provider") or ""), value)
    name = value[1:].strip()
    if not name:
        raise ModelPresetError("model reference '@' is empty")
    if name in _stack:
        raise ModelPresetError("model reference cycle: " + " -> ".join((*_stack, name)))
    if name == "default":
        if model_cfg.get("model_preset"):
            raw_model = "@" + str(model_cfg["model_preset"])
        else:
            raw_model = model_cfg.get("default") or model_cfg.get("model") or ""
        if isinstance(raw_model, (Mapping, str)) and (isinstance(raw_model, Mapping) or str(raw_model).startswith("@")):
            route = resolve_model_ref(raw_model, cfg, _stack=(*_stack, name))
            return ModelPresetRoute(name, str(model_cfg.get("provider") or route.provider), route.model,
                                    str(model_cfg.get("reasoning_effort") or route.reasoning_effort), route.fallbacks)
        return ModelPresetRoute(name, str(model_cfg.get("provider") or ""), str(raw_model), str(model_cfg.get("reasoning_effort") or ""))
    if name == "attended":
        attended_value = model_cfg.get("attended")
        attended: Mapping[str, Any] = attended_value if isinstance(attended_value, Mapping) else {}
        raw_model = attended.get("model") or ("@" + str(attended.get("model_preset")) if attended.get("model_preset") else "")
        if isinstance(raw_model, (Mapping, str)) and (isinstance(raw_model, Mapping) or str(raw_model).startswith("@")):
            route = resolve_model_ref(raw_model, cfg, _stack=(*_stack, name))
            return ModelPresetRoute(name, str(attended.get("provider") or route.provider), route.model,
                                    str(attended.get("reasoning_effort") or route.reasoning_effort), route.fallbacks)
        return ModelPresetRoute(name, str(attended.get("provider") or ""), str(raw_model), str(attended.get("reasoning_effort") or ""))
    presets = cfg.get("model_presets")
    entry = presets.get(name) if isinstance(presets, dict) else None
    if not isinstance(entry, dict):
        raise ModelPresetError(f"model_presets: unknown preset '{name}'")
    raw_model = entry.get("model")
    if isinstance(raw_model, str) and raw_model.startswith("@"):
        base = resolve_model_ref(raw_model, cfg, _stack=(*_stack, name))
        provider = str(entry.get("provider") or base.provider)
        model = base.model
        effort = str(entry.get("reasoning_effort") or base.reasoning_effort)
        fallbacks = base.fallbacks
    else:
        provider = str(entry.get("provider") or "").strip()
        model = str(raw_model or "").strip()
        effort = str(entry.get("reasoning_effort") or "")
        fallbacks = tuple(entry.get("fallbacks") or ())
    if not provider or not model:
        raise ModelPresetError(f"model_presets.{name}: provider and model are required")
    return ModelPresetRoute(name, provider, model, effort, tuple(fallbacks))


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
        if isinstance(raw, str) and raw.startswith("@"):
            try:
                resolve_model_ref(raw, cfg)
            except ModelPresetError as exc:
                errors.append(f"{path}: {exc}")
    return errors
