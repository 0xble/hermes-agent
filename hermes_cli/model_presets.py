"""Static expansion of named model routes in configuration.

Model presets are authoring conveniences, not runtime providers: every consumer receives
an ordinary inline route before provider and credential resolution begins.
"""
from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from hermes_cli.config_errors import ConfigValidationError


_ROUTE_KEYS = frozenset({"provider", "model", "reasoning_effort", "fallbacks"})
_MAIN_FORBIDDEN_REFERENCE_FIELDS = frozenset({
    # Presets select provider/model only. Credentials and endpoints are provider-owned;
    # accepting them here would silently discard a user setting during expansion.
    "base_url", "api_base", "api_key", "api", "key_env", "api_key_env", "api_mode", "transport",
})
_EMPTY_DEFAULT_ROUTE_FIELDS = _MAIN_FORBIDDEN_REFERENCE_FIELDS | frozenset({"extra_body"})


_PRESET_REF = re.compile(r"^@([A-Za-z0-9_-]+)$")


def preset_ref_name(value: Any) -> str | None:
    """Return the preset name when ``value`` is a bare ``@name`` reference, else ``None``.

    Anything else starting with ``@`` (``@cf/meta/llama-3.3-70b``, ``@hf/...``) is a literal
    model ID: a reference never contains ``/``, ``.`` or ``:``.
    """
    if not isinstance(value, str):
        return None
    match = _PRESET_REF.match(value.strip())
    return match.group(1) if match else None


class ModelPresetError(ConfigValidationError):
    """A model preset cannot be expanded safely."""


ReasoningEffort = str | dict[str, Any]


def normalize_route_reasoning_effort(value: Any) -> ReasoningEffort:
    """Keep valid route policy typed and make disabled values truthy, never absent.

    Reuse the reasoning parser so YAML false and disabled dictionaries share the
    same semantics. Enabled dictionaries retain provider-specific effort names.
    """
    from hermes_constants import parse_reasoning_effort

    parsed = parse_reasoning_effort(value)
    if parsed is None:
        return ""
    if not parsed["enabled"]:
        return "none"
    return parsed if isinstance(value, dict) else parsed["effort"]


@dataclass(frozen=True)
class ModelPresetRoute:
    name: str
    provider: str
    model: str
    reasoning_effort: ReasoningEffort = ""
    fallbacks: tuple[dict[str, Any], ...] = ()


def _error(path: str, message: str) -> ModelPresetError:
    return ModelPresetError(f"model_presets: {path}: {message}")


def _text(value: Any, path: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(path, f"'{field}' must be a non-empty string")
    return value.strip()


def _validate_reasoning(value: Any, path: str) -> None:
    if value is None:
        return
    from hermes_constants import parse_reasoning_effort
    if parse_reasoning_effort(value) is None:
        raise _error(path, "'reasoning_effort' must be one of none, minimal, low, medium, high, xhigh, max, ultra")


def _validate_route(value: Any, path: str, *, allow_fallbacks: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, "must be a mapping")
    unknown = set(value) - _ROUTE_KEYS
    if unknown:
        raise _error(path, "unsupported field(s): " + ", ".join(sorted(map(str, unknown))))
    route: dict[str, Any] = {
        "provider": _text(value.get("provider"), path, "provider"),
        "model": _text(value.get("model"), path, "model"),
    }
    if preset_ref_name(route["model"]):
        raise _error(path, "preset routes must be flat, not nested @ references")
    if "reasoning_effort" in value:
        _validate_reasoning(value["reasoning_effort"], path)
        route["reasoning_effort"] = normalize_route_reasoning_effort(value["reasoning_effort"])
    if "fallbacks" in value:
        if not allow_fallbacks:
            raise _error(path, "fallbacks are not supported in a fallback route")
        fallbacks = value["fallbacks"]
        if not isinstance(fallbacks, list):
            raise _error(path, "'fallbacks' must be an ordered list of inline routes")
        route["fallbacks"] = [
            _validate_route(item, f"{path}.fallbacks[{index}]", allow_fallbacks=False)
            for index, item in enumerate(fallbacks)
        ]
    return route


def _definitions(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = config.get("model_presets")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _error("model_presets", "must be a mapping of names to flat route definitions")
    definitions: dict[str, dict[str, Any]] = {}
    for name, route in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise _error("model_presets", "preset names must be non-empty strings")
        clean_name = name.strip()
        if clean_name.lower() in {"attended", "default"}:
            raise _error("model_presets", f"preset name '{clean_name}' is reserved")
        if preset_ref_name("@" + clean_name) != clean_name:
            raise _error("model_presets", "preset names may contain only letters, digits, '_' or '-'")
        if clean_name in definitions:
            raise _error("model_presets", f"duplicate preset name '{clean_name}'")
        definitions[clean_name] = _validate_route(route, f"model_presets.{clean_name}", allow_fallbacks=True)
    return definitions


def _reference(site: Any, path: str, definitions: dict[str, dict[str, Any]], *, conflicts: set[str]) -> dict[str, Any] | None:
    # String references are the shorthand accepted at every reference site. Normalize them
    # here so all sites retain identical validation, conflict, and expansion behavior.
    if preset_ref_name(site):
        site = {"model_preset": preset_ref_name(site)}
    if not isinstance(site, dict) or "model_preset" not in site:
        return None
    name = _text(site["model_preset"], path, "model_preset")
    inline = sorted(str(key) for key in set(site).intersection(conflicts))
    if inline:
        raise _error(path, f"'model_preset: {name}' cannot be combined with inline route field(s): {', '.join(inline)}")
    route = definitions.get(name)
    if route is None:
        available = ", ".join(definitions) or "(none)"
        raise _error(path, f"unknown preset '{name}'. Available presets: {available}")
    return deepcopy(route)


def _fallbacks_disabled(site: Any, path: str) -> bool:
    """The sole reference-site override: ``fallbacks: []`` disables a preset chain."""
    if not isinstance(site, dict) or "model_preset" not in site or "fallbacks" not in site:
        return False
    if site["fallbacks"] == []:
        return True
    raise _error(path, "'fallbacks' may only be [] with model_preset (to disable preset fallbacks)")


def _route_fields(route: dict[str, Any], *, fallback_key: str | None = None) -> dict[str, Any]:
    result = {key: deepcopy(route[key]) for key in ("provider", "model", "reasoning_effort") if key in route}
    if "fallbacks" in route:
        if fallback_key is None:
            raise _error("route", "fallbacks are unsupported at this reference site")
        result[fallback_key] = deepcopy(route["fallbacks"])
    return result


def _lift_model_shorthand(site: Any) -> Any:
    """Treat ``@name`` (bare, or as the ``model`` value of a mapping) as ``{model_preset: name}``."""
    if preset_ref_name(site):
        return {"model_preset": preset_ref_name(site)}
    if isinstance(site, dict) and "model_preset" not in site:
        name = preset_ref_name(site.get("model"))
        if name:
            lifted = {k: v for k, v in site.items() if k != "model"}
            lifted["model_preset"] = name
            return lifted
    return site


def _in_authored_form(original: Any, restored: Any, keys: tuple[str, ...] = ("model",)) -> Any:
    """Put a restored ``{model_preset: X}`` site back in the shorthand the user wrote.

    ``original`` is the authored site (``"@X"`` string, or a mapping carrying ``@X`` in one of
    ``keys``). Mapping-form references (``model_preset: X``) are already restored verbatim.
    """
    if not isinstance(restored, dict) or "model_preset" not in restored:
        return restored
    if isinstance(original, str) and preset_ref_name(original):
        return original if set(restored) == {"model_preset"} else restored
    if isinstance(original, dict) and "model_preset" not in original:
        for key in keys:
            if preset_ref_name(original.get(key)):
                result = {k: v for k, v in restored.items() if k != "model_preset"}
                result[key] = original[key]
                return result
    return restored


def _expand_model(config: dict[str, Any], definitions: dict[str, dict[str, Any]]) -> None:
    model = config.get("model")
    if preset_ref_name(model):
        model = {"model_preset": preset_ref_name(model)}
        config["model"] = model
    if isinstance(model, dict):
        for key in ("default", "model"):
            name = preset_ref_name(model.get(key))
            if "model_preset" not in model and name:
                model["model_preset"] = name
                model.pop(key, None)
                break
    if not isinstance(model, dict):
        return
    disabled_fallbacks = _fallbacks_disabled(model, "model")
    route = _reference(
        model, "model", definitions,
        conflicts={"provider", "default", "model", "reasoning_effort", *_MAIN_FORBIDDEN_REFERENCE_FIELDS},
    )
    if route is None:
        return
    # Preserve non-route model metadata such as context_length.
    config["model"] = {
        **{key: deepcopy(value) for key, value in model.items() if key not in {"model_preset", "fallbacks"}},
        "provider": route["provider"], "default": route["model"],
    }
    if "reasoning_effort" in route:
        # Keep the preset effort on the selected route. It must not become a global
        # agent default, because that would affect unrelated models.
        config["model"]["reasoning_effort"] = deepcopy(route["reasoning_effort"])
    if disabled_fallbacks:
        # Explicit opt-out must survive raw gateway loads and default/overlay inheritance.
        config["fallback_providers"] = []
    elif "fallbacks" in route:
        if "fallback_providers" in config:
            raise _error("model", "preset fallbacks cannot be combined with top-level fallback_providers (including an empty list)")
        config["fallback_providers"] = deepcopy(route["fallbacks"])


def _expand_delegation(config: dict[str, Any], definitions: dict[str, dict[str, Any]]) -> None:
    site = config.get("delegation")
    site = _lift_model_shorthand(site)
    disabled_fallbacks = _fallbacks_disabled(site, "delegation")
    route = _reference(site, "delegation", definitions, conflicts={"provider", "model", "base_url", "api_key", "reasoning_effort", "fallback_providers", "fallback_chain"})
    if route is not None:
        assert isinstance(site, dict)
        fields = _route_fields({key: value for key, value in route.items() if key != "fallbacks"} if disabled_fallbacks else route, fallback_key="fallback_providers")
        if disabled_fallbacks:
            fields["fallback_providers"] = []
        config["delegation"] = {**{key: value for key, value in site.items() if key not in {"model_preset", "fallbacks"}}, **fields}
    delegation = config.get("delegation")
    if not isinstance(delegation, dict):
        return
    _expand_delegation_by_parent(delegation, definitions)
    if not isinstance(delegation.get("subagents"), dict):
        return
    roles = delegation["subagents"]
    for name, role in list(roles.items()):
        role = _lift_model_shorthand(role)
        roles[name] = role
        path = f"delegation.subagents.{name}"
        disabled_fallbacks = _fallbacks_disabled(role, path)
        route = _reference(
            role, path, definitions,
            conflicts={"provider", "model", "reasoning_effort"},
        )
        if route is None:
            continue
        assert isinstance(role, dict)
        fields = _route_fields(
            {key: value for key, value in route.items() if key != "fallbacks"}
            if disabled_fallbacks else route,
            fallback_key="fallbacks",
        )
        if disabled_fallbacks:
            fields["fallbacks"] = []
        roles[name] = {
            **{key: value for key, value in role.items() if key not in {"model_preset", "fallbacks"}},
            **fields,
        }


def _expand_delegation_by_parent(delegation: dict[str, Any], definitions: dict[str, dict[str, Any]]) -> None:
    if "by_parent" not in delegation:
        return
    raw = delegation["by_parent"]
    if not isinstance(raw, dict):
        raise _error("delegation.by_parent", "must be a mapping")
    expanded: dict[str, Any] = {}
    for key, value in raw.items():
        path = f"delegation.by_parent.{key}"
        if not isinstance(key, str) or not key.strip():
            raise _error(path, "parent model must be a non-empty string")
        parent_route = _reference(key, path, definitions, conflicts=set())
        model = parent_route["model"] if parent_route is not None else key
        if model in expanded:
            raise _error(path, f"duplicate parent model '{model}' after preset expansion")
        if value == "inherit":
            expanded[model] = "inherit"
        elif preset_ref_name(value):
            _reference(value, path, definitions, conflicts=set())
            # Retain target references so repeated expansion (e.g. fallback
            # resolution on CLI_CONFIG) remains valid and serializable.
            expanded[model] = value
        else:
            raise _error(path, "value must be 'inherit' or an @preset reference")
    delegation["by_parent"] = expanded


def delegation_runtime_config(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve validated parent-map targets only at the delegation boundary.

    General config retains string values so it can be expanded repeatedly,
    serialized, and saved without admitting inline dictionaries in authored maps.
    """
    cfg = config.get("delegation") or {}
    if not isinstance(cfg, dict):
        return {}
    routes = cfg.get("by_parent", {})
    if not any(preset_ref_name(value) for value in routes.values()):
        return cfg
    definitions = _definitions(config)
    resolved = {}
    for model, value in routes.items():
        route = _reference(value, f"delegation.by_parent.{model}", definitions, conflicts=set())
        resolved[model] = _route_fields(route, fallback_key="fallback_providers") if route is not None else value
    return {**cfg, "by_parent": resolved}


def _expand_auxiliary(config: dict[str, Any], definitions: dict[str, dict[str, Any]]) -> None:
    auxiliary = config.get("auxiliary")
    if not isinstance(auxiliary, dict):
        return
    for task, site in list(auxiliary.items()):
        path = f"auxiliary.{task}"
        site = _lift_model_shorthand(site)
        auxiliary[task] = site
        disabled_fallbacks = _fallbacks_disabled(site, path)
        route = _reference(site, path, definitions, conflicts={"provider", "model", "base_url", "api_key", "api_mode", "reasoning_effort", "fallback_chain", "fallback_providers"})
        if route is not None:
            assert isinstance(site, dict)
            fields = _route_fields({key: value for key, value in route.items() if key != "fallbacks"} if disabled_fallbacks else route, fallback_key="fallback_chain")
            if disabled_fallbacks:
                fields["fallback_chain"] = []
            auxiliary[task] = {**{key: value for key, value in site.items() if key not in {"model_preset", "fallbacks"}}, **fields}


def _expand_fallback_entries(config: dict[str, Any], definitions: dict[str, dict[str, Any]]) -> None:
    for key in ("fallback_providers", "fallback_model"):
        raw = config.get(key)
        entries = raw if isinstance(raw, list) else [raw] if isinstance(raw, (dict, str)) else None
        if entries is None:
            continue
        expanded = []
        for index, entry in enumerate(entries):
            entry = _lift_model_shorthand(entry)
            route = _reference(entry, f"{key}[{index}]", definitions, conflicts={"provider", "model", "base_url", "api_key", "key_env", "api_key_env", "reasoning_effort", "fallbacks", "fallback_chain", "fallback_providers"})
            if route is not None:
                if "fallbacks" in route:
                    raise _error(f"{key}[{index}]", "a fallback entry cannot reference a preset that declares fallbacks")
                expanded.append(_route_fields(route))
            else:
                expanded.append(entry)
        config[key] = expanded if isinstance(raw, list) else expanded[0]


def _expand_moa_slot(
    site: Any, path: str, definitions: dict[str, dict[str, Any]], *, fallback: bool = False,
) -> Any:
    site = _lift_model_shorthand(site)
    if not isinstance(site, dict):
        return site
    if "model_preset" not in site:
        if not fallback and isinstance(site.get("fallback_models"), list):
            site["fallback_models"] = [
                _expand_moa_slot(candidate, f"{path}.fallback_models[{index}]", definitions, fallback=True)
                for index, candidate in enumerate(site["fallback_models"])
            ]
        return site
    disabled_fallbacks = _fallbacks_disabled(site, path)
    if "fallback_models" in site:
        if site["fallback_models"] != []:
            raise _error(path, "'fallback_models' may only be [] with model_preset (to disable preset fallbacks)")
        disabled_fallbacks = True
    route = _reference(site, path, definitions, conflicts={"provider", "model", "reasoning_effort"})
    if route is None:
        return site
    if fallback and "fallbacks" in route:
        raise _error(path, "a MoA slot cannot reference a preset that declares fallbacks")
    fields = _route_fields(
        {key: value for key, value in route.items() if key != "fallbacks"}
        if disabled_fallbacks else route,
        fallback_key="fallback_models",
    )
    if disabled_fallbacks:
        fields["fallback_models"] = []
    result = {
        **{key: value for key, value in site.items() if key not in {"model_preset", "fallback_models"}},
        **fields,
    }
    if not fallback and isinstance(result.get("fallback_models"), list):
        result["fallback_models"] = [
            _expand_moa_slot(candidate, f"{path}.fallback_models[{index}]", definitions, fallback=True)
            for index, candidate in enumerate(result["fallback_models"])
        ]
    return result


def _expand_moa(config: dict[str, Any], definitions: dict[str, dict[str, Any]]) -> None:
    moa = config.get("moa")
    if not isinstance(moa, dict):
        return
    named_presets = isinstance(moa.get("presets"), dict)
    blocks = moa["presets"] if named_presets else {"default": moa}
    for name, block in blocks.items():
        if not isinstance(block, dict):
            continue
        prefix = f"moa.presets.{name}" if named_presets else "moa"
        refs = block.get("reference_models")
        if isinstance(refs, list):
            block["reference_models"] = [_expand_moa_slot(slot, f"{prefix}.reference_models[{index}]", definitions) for index, slot in enumerate(refs)]
        if "aggregator" in block:
            block["aggregator"] = _expand_moa_slot(block["aggregator"], f"{prefix}.aggregator", definitions)


def expand_model_presets(config: Any) -> dict[str, Any]:
    """Return a copied config with every supported ``model_preset`` reference expanded.

    The source mapping is never mutated so callers that save configuration retain the user's
    named references rather than a flattened implementation detail.
    """
    if not isinstance(config, dict):
        return config
    expanded = deepcopy(config)
    definitions = _definitions(expanded)
    _expand_model(expanded, definitions)
    _expand_delegation(expanded, definitions)
    _expand_auxiliary(expanded, definitions)
    _expand_fallback_entries(expanded, definitions)
    _expand_moa(expanded, definitions)
    return expanded


def _lift_main_model(model: Any) -> Any:
    """Authored top-level ``model`` with any ``@X`` shorthand lifted to ``model_preset``."""
    name = preset_ref_name(model)
    if name:
        return {"model_preset": name}
    if isinstance(model, dict) and "model_preset" not in model:
        for key in ("default", "model"):
            name = preset_ref_name(model.get(key))
            if name:
                lifted = {k: v for k, v in model.items() if k != key}
                lifted["model_preset"] = name
                return lifted
    return model


def _restore_parent_routes(actual: Any, raw: Any, expected: Any, definitions: dict[str, Any]) -> None:
    if not all(isinstance(site, dict) for site in (actual, raw, expected)):
        return
    # Restore entries individually so adding or editing one route cannot persist
    # unrelated expanded target dictionaries, which are not valid authored values.
    for key, value in raw.items():
        parent = _reference(key, f"delegation.by_parent.{key}", definitions, conflicts=set())
        model = parent["model"] if parent is not None else key
        if model not in actual or (key != model and key in actual):
            continue
        current = actual.pop(model)
        actual[key] = deepcopy(value if current == expected[model] else current)


def preserve_model_preset_references(config: dict[str, Any], authored: Any) -> dict[str, Any]:
    """Put unchanged named-route references back before a config write.

    ``load_config`` returns runtime-ready inline routes.  An unrelated config edit must not
    turn those routes into permanently flattened YAML, but an edited route must remain edited.
    """
    if not isinstance(authored, dict) or not authored.get("model_presets"):
        return config
    expanded_authored = expand_model_presets(authored)
    result = deepcopy(config)

    def same_route(actual: Any, expected: Any, fallback_key: str | None = None) -> bool:
        if not isinstance(actual, dict) or not isinstance(expected, dict):
            return False
        keys = {"provider", "model", "reasoning_effort"} | _EMPTY_DEFAULT_ROUTE_FIELDS
        def value(site: dict[str, Any], key: str) -> Any:
            item = site.get(key)
            return None if item in (None, "", {}, []) else item
        if any(value(actual, key) != value(expected, key) for key in keys):
            return False
        return fallback_key is None or value(actual, fallback_key) == value(expected, fallback_key)

    original_model = authored.get("model")
    raw_model = _lift_main_model(original_model)
    expected_model = expanded_authored.get("model")
    if isinstance(raw_model, dict) and "model_preset" in raw_model and isinstance(expected_model, dict):
        actual_model = result.get("model")
        unsafe_model_fields = any(
            key in actual_model and key not in raw_model and actual_model[key] not in (None, "", {}, [])
            for key in _MAIN_FORBIDDEN_REFERENCE_FIELDS
        ) if isinstance(actual_model, dict) else True
        preset = _definitions(authored)[raw_model["model_preset"].strip()]
        fallbacks_unchanged = (
            "fallbacks" not in preset and "fallbacks" not in raw_model
            or result.get("fallback_providers") == expanded_authored.get("fallback_providers")
        )
        if (isinstance(actual_model, dict) and not unsafe_model_fields
                and fallbacks_unchanged
                and actual_model.get("provider") == expected_model.get("provider") and actual_model.get("default") == expected_model.get("default")
                and ("reasoning_effort" not in actual_model or actual_model.get("reasoning_effort") == preset.get("reasoning_effort"))):
            restored_model = deepcopy(actual_model)
            restored_model.pop("provider", None)
            restored_model.pop("default", None)
            if restored_model.get("reasoning_effort") == preset.get("reasoning_effort"):
                restored_model.pop("reasoning_effort", None)
            for key in _MAIN_FORBIDDEN_REFERENCE_FIELDS:
                if key not in raw_model and restored_model.get(key) in (None, "", {}, []):
                    restored_model.pop(key, None)
            restored_model["model_preset"] = raw_model["model_preset"]
            if raw_model.get("fallbacks") == []:
                restored_model["fallbacks"] = []
            result["model"] = _in_authored_form(original_model, restored_model, ("default", "model"))
            if (("fallbacks" in preset or "fallbacks" in raw_model)
                    and "fallback_providers" not in authored
                    and result.get("fallback_providers") == expanded_authored.get("fallback_providers")):
                result.pop("fallback_providers", None)

    def restore_site(container: dict[str, Any], raw_container: Any, expected_container: Any, key: str, fallback_key: str | None = None) -> None:
        original_site = raw_container.get(key) if isinstance(raw_container, dict) else None
        raw_site = _lift_model_shorthand(original_site)
        expected_site = expected_container.get(key) if isinstance(expected_container, dict) else None
        actual_site = container.get(key)
        if isinstance(raw_site, dict) and "model_preset" in raw_site and same_route(actual_site, expected_site, fallback_key):
            assert isinstance(actual_site, dict)
            # Keep edits to unrelated site settings (timeouts, concurrency, MoA enabled),
            # but remove only fields injected by expansion before restoring the reference.
            restored = deepcopy(actual_site)
            for route_key in ("provider", "model", "reasoning_effort"):
                if isinstance(expected_site, dict) and route_key in expected_site:
                    restored.pop(route_key, None)
            # Matching empty fallback defaults are also inert expansion output.
            if fallback_key:
                restored.pop(fallback_key, None)
            # strip_defaults=False exposes empty provider-owned defaults which would otherwise
            # become inline route fields and make a saved reference fail on its next load.
            for route_key in _EMPTY_DEFAULT_ROUTE_FIELDS:
                if route_key not in raw_site and restored.get(route_key) in (None, "", {}, []):
                    restored.pop(route_key, None)
            restored["model_preset"] = raw_site["model_preset"]
            if raw_site.get("fallbacks") == []:
                restored["fallbacks"] = []
            container[key] = _in_authored_form(original_site, restored)

    restore_site(result, authored, expanded_authored, "delegation", "fallback_providers")
    actual_delegation = result.get("delegation")
    raw_delegation = authored.get("delegation")
    expected_delegation = expanded_authored.get("delegation")
    if (isinstance(actual_delegation, dict) and isinstance(raw_delegation, dict)
            and isinstance(expected_delegation, dict)):
        _restore_parent_routes(
            actual_delegation.get("by_parent"), raw_delegation.get("by_parent"),
            expected_delegation.get("by_parent"), _definitions(authored),
        )
        raw_roles = raw_delegation.get("subagents")
        actual_roles = actual_delegation.get("subagents")
        expected_roles = expected_delegation.get("subagents")
        if isinstance(raw_roles, dict) and isinstance(actual_roles, dict) and isinstance(expected_roles, dict):
            for name, raw_role in raw_roles.items():
                restore_site(actual_roles, raw_roles, expected_roles, name, "fallbacks")
    actual = result.get("auxiliary")
    raw = authored.get("auxiliary")
    expected = expanded_authored.get("auxiliary")
    if isinstance(actual, dict) and isinstance(raw, dict):
        for key in raw:
            restore_site(actual, raw, expected, key, "fallback_chain")

    def restore_slot(actual_slot: Any, raw_slot: Any, expected_slot: Any, *, shorthand: bool = False) -> Any:
        original_slot = raw_slot
        if shorthand:
            raw_slot = _lift_model_shorthand(raw_slot)
        if not (isinstance(actual_slot, dict) and isinstance(raw_slot, dict) and isinstance(expected_slot, dict)):
            return actual_slot
        if "model_preset" not in raw_slot:
            restored = deepcopy(actual_slot)
            raw_fbs = raw_slot.get("fallback_models")
            actual_fbs = actual_slot.get("fallback_models")
            expected_fbs = expected_slot.get("fallback_models")
            if (isinstance(raw_fbs, list) and isinstance(actual_fbs, list) and isinstance(expected_fbs, list)
                    and len(raw_fbs) == len(actual_fbs) == len(expected_fbs)):
                restored["fallback_models"] = [
                    restore_slot(actual, raw, expected)
                    for actual, raw, expected in zip(actual_fbs, raw_fbs, expected_fbs)
                ]
            return restored
        if not same_route(actual_slot, expected_slot, "fallback_models"):
            return actual_slot
        restored = deepcopy(actual_slot)
        for route_key in ("provider", "model", "reasoning_effort"):
            if isinstance(expected_slot, dict) and route_key in expected_slot:
                restored.pop(route_key, None)
        if isinstance(expected_slot, dict) and "fallback_models" in expected_slot:
            restored.pop("fallback_models", None)
        restored["model_preset"] = raw_slot["model_preset"]
        if raw_slot.get("fallback_models") == []:
            restored["fallback_models"] = []
        return _in_authored_form(original_slot, restored) if shorthand else restored

    for fallback_key in ("fallback_providers", "fallback_model"):
        raw_entries = authored.get(fallback_key)
        actual_entries = result.get(fallback_key)
        expected_entries = expanded_authored.get(fallback_key)
        raw_list = raw_entries if isinstance(raw_entries, list) else [raw_entries] if isinstance(raw_entries, (dict, str)) else None
        actual_list = actual_entries if isinstance(actual_entries, list) else [actual_entries] if isinstance(actual_entries, dict) else None
        expected_list = expected_entries if isinstance(expected_entries, list) else [expected_entries] if isinstance(expected_entries, dict) else None
        if raw_list is not None and actual_list is not None and expected_list is not None and len(raw_list) == len(actual_list) == len(expected_list):
            restored = [restore_slot(actual, raw, expected, shorthand=True) for actual, raw, expected in zip(actual_list, raw_list, expected_list)]
            result[fallback_key] = restored if isinstance(actual_entries, list) else restored[0]

    raw_moa, actual_moa, expected_moa = authored.get("moa"), result.get("moa"), expanded_authored.get("moa")
    if isinstance(raw_moa, dict) and isinstance(actual_moa, dict) and isinstance(expected_moa, dict):
        raw_blocks = raw_moa.get("presets") if isinstance(raw_moa.get("presets"), dict) else {"default": raw_moa}
        actual_blocks = actual_moa.get("presets") if isinstance(actual_moa.get("presets"), dict) else {"default": actual_moa}
        expected_blocks = expected_moa.get("presets") if isinstance(expected_moa.get("presets"), dict) else {"default": expected_moa}
        for name, raw_block in raw_blocks.items():
            actual_block, expected_block = actual_blocks.get(name), expected_blocks.get(name)
            if not (isinstance(raw_block, dict) and isinstance(actual_block, dict) and isinstance(expected_block, dict)):
                continue
            assert isinstance(actual_block, dict) and isinstance(expected_block, dict)
            for slot_key in ("aggregator",):
                actual_block[slot_key] = restore_slot(actual_block.get(slot_key), raw_block.get(slot_key), expected_block.get(slot_key))
            raw_refs, actual_refs, expected_refs = raw_block.get("reference_models"), actual_block.get("reference_models"), expected_block.get("reference_models")
            if isinstance(raw_refs, list) and isinstance(actual_refs, list) and isinstance(expected_refs, list) and len(raw_refs) == len(actual_refs) == len(expected_refs):
                actual_block["reference_models"] = [restore_slot(actual, raw, expected) for actual, raw, expected in zip(actual_refs, raw_refs, expected_refs)]
    return result
