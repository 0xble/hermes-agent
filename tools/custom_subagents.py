"""Trusted named delegation definitions, independent of child execution.

Only configuration selects model/provider/effort. Model-facing calls select an
identifier, never credentials or arbitrary runtime settings. Parsed definitions
are immutable snapshots, so editing configuration cannot alter a running child.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from agent.credential_pool import CredentialPool
from copy import deepcopy
import re
from typing import Mapping

from agent.reasoning_effort import EFFORT_LADDER


@dataclass(frozen=True, slots=True)
class FallbackDefinition:
    provider: str
    model: str
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class SubagentDefinition:
    name: str
    description: str
    instructions: str
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    inherit_parent: bool = False
    context_mode: str = "fresh"
    moa_presets: tuple[str, ...] | None = None
    fallbacks: tuple[FallbackDefinition, ...] | None = None


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    """One fully authorized runtime route frozen for a child launch.

    The API key is runtime-only and excluded from repr/comparison/metadata.  The
    digest is retained for the final physical-request guard.
    """
    provider: str
    model: str
    base_url: str
    api_mode: str
    reasoning_effort: str | None
    api_key: str | None = field(default=None, repr=False, compare=False)
    credential_digest: str = field(default="", repr=False)
    request_overrides_json: str = "{}"
    _credential_pool: CredentialPool | None = field(default=None, repr=False, compare=False)
    credential_pool_entry_id: str | None = None

    def native_entry(self) -> dict:
        entry = {
            "provider": self.provider, "model": self.model,
            "base_url": self.base_url, "api_mode": self.api_mode,
        }
        if self.reasoning_effort is not None:
            entry["reasoning_effort"] = self.reasoning_effort
        if self.api_key:
            entry["api_key"] = self.api_key
        entry["request_overrides"] = json.loads(self.request_overrides_json)
        return entry

    def metadata(self) -> dict:
        metadata = {
            "provider": self.provider, "model": self.model,
            "base_url": nonsecret_route_url(self.base_url), "api_mode": self.api_mode,
            "reasoning_effort": self.reasoning_effort,
            "authority_fingerprint": self.credential_digest,
            "request_overrides": _nonsecret_mapping(json.loads(self.request_overrides_json)),
            "request_overrides_fingerprint": _authority_mapping_fingerprint(
                json.loads(self.request_overrides_json)
            ),
        }
        if self.credential_pool_entry_id:
            metadata["credential_pool_entry_id"] = self.credential_pool_entry_id
        return metadata


def nonsecret_route_url(value: str) -> str:
    """Credential-free canonical URL suitable for durable launch metadata."""
    try:
        parsed = urlsplit(str(value or ""))
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (TypeError, ValueError):
        return ""


_NONSECRET_TOKEN_LIMIT_KEYS = frozenset({
    "max_tokens", "max_output_tokens", "max_completion_tokens",
    "reference_max_tokens", "token_limit", "input_token_limit",
    "output_token_limit", "context_token_limit",
})


def _mapping_key_is_secret(key, value) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    if (
        normalized in _NONSECRET_TOKEN_LIMIT_KEYS
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    ):
        return False
    return any(marker in normalized for marker in (
        "api_key", "token", "credential", "authorization", "headers", "cookie",
    ))


def _nonsecret_mapping(value):
    if isinstance(value, dict):
        return {key: _nonsecret_mapping(item) for key, item in value.items()
                if not _mapping_key_is_secret(key, item)}
    if isinstance(value, list):
        return [_nonsecret_mapping(item) for item in value]
    return value


def _authority_mapping_fingerprint(value) -> str:
    """Bind complete override authority without persisting its secret values."""
    canonical = json.dumps(
        value or {}, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(b"hermes-request-overrides-v1\0" + canonical).hexdigest()


def _authority_mapping_matches(value, public_value, fingerprint) -> bool:
    """Validate full authority; legacy metadata is safe only when nothing was redacted."""
    if isinstance(fingerprint, str) and fingerprint:
        return _authority_mapping_fingerprint(value) == fingerprint
    return (value or {}) == (public_value or {})


@dataclass(frozen=True, slots=True)
class ResolvedSubagentLaunch:
    definition: SubagentDefinition | None
    credentials: Mapping
    reasoning: Mapping | None
    fallback_routes: tuple[ResolvedRoute, ...] = ()
    moa_snapshot: object | None = None
    resume_session_id: str | None = None
    enabled_toolsets: tuple[str, ...] | None = None
    workspace_path: str | None = None
    launch_metadata: Mapping | None = None
    resume_claim_id: str | None = None
    _credential_pool: CredentialPool | None = field(default=None, repr=False, compare=False)
    resume_credential_id: str | None = None
    resume_recovery: Mapping | None = None


_FIELDS = frozenset({"description", "instructions", "provider", "model", "reasoning_effort", "inherit_parent", "moa_presets", "fallbacks", "context_mode"})
# Public aliases for the configuration system, which validates
# ``delegation.subagents.<name>.<field>`` without importing the runtime.
SUBAGENT_FIELDS = _FIELDS
SUBAGENT_NAME_PATTERN = r"[a-z][a-z0-9_-]*"
_NAME = re.compile(SUBAGENT_NAME_PATTERN + r"\Z")
_AUTH_HEADER_NAMES = frozenset({"authorization", "chatgpt-account-id"})


def _route_host(base_url: str) -> tuple[str, str]:
    """Scheme and host of a base URL, ignoring SDK path normalization.

    A real route change moves the request to a different endpoint; a ``/v1``
    that the SDK keeps or drops does not.
    """
    parts = urlsplit(str(base_url or ""))
    return (parts.scheme.lower(), parts.netloc.lower())


def _same_pinned_base_url(left: str, right: str) -> bool:
    """Compare frozen base URLs with the shared route normalizer, not raw spelling."""
    from hermes_cli.route_identity import normalize_route_base_url
    return normalize_route_base_url(left) == normalize_route_base_url(right)


def _same_pinned_route(current: tuple, provider: str, model: str, base_url: str, api_mode: str) -> bool:
    return (
        current[0] == provider
        and current[1] == model
        and current[3] == api_mode
        and _same_pinned_base_url(current[2], base_url)
    )


def parse_definitions(config: Mapping) -> dict[str, SubagentDefinition]:
    """Validate the entire registry before any child can be constructed."""
    if config.get("subagents") is None:
        return {}
    raw = config["subagents"]
    if not isinstance(raw, dict):
        raise ValueError("delegation.subagents must be a mapping")
    definitions = {}
    for name, fields in raw.items():
        where = f"delegation.subagents.{name}"
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f"{where}: expected a lowercase subagent identifier")
        if not isinstance(fields, dict):
            raise ValueError(f"{where} must be a mapping")
        unknown = fields.keys() - _FIELDS
        if unknown:
            raise ValueError(f"{where}: unknown fields: {', '.join(sorted(map(str, unknown)))}")
        for required in ("description", "instructions"):
            if required not in fields:
                raise ValueError(f"{where}.{required} is required")
        for key, value in fields.items():
            if key == "inherit_parent":
                if not isinstance(value, bool):
                    raise ValueError(f"{where}.inherit_parent must be a boolean")
                continue
            if key == "moa_presets":
                if not isinstance(value, list) or not value or any(not isinstance(p, str) or not p.strip() for p in value):
                    raise ValueError(f"{where}.moa_presets must be a nonempty list of preset names")
                if len(set(value)) != len(value):
                    raise ValueError(f"{where}.moa_presets must not contain duplicates")
                continue
            if key == "fallbacks":
                if not isinstance(value, list) or any(not isinstance(route, dict) for route in value):
                    raise ValueError(f"{where}.fallbacks must be a list of route mappings")
                fallback_ids = set()
                for route in value:
                    if set(route) - {"provider", "model", "reasoning_effort"} or not isinstance(route.get("provider"), str) or not isinstance(route.get("model"), str):
                        raise ValueError(f"{where}.fallbacks entries require provider and model, with optional reasoning_effort")
                    if not route["provider"].strip() or not route["model"].strip() or route["provider"] != route["provider"].strip() or route["model"] != route["model"].strip():
                        raise ValueError(f"{where}.fallbacks provider/model must be nonempty without surrounding whitespace")
                    route_id = (route["provider"], route["model"])
                    if route_id in fallback_ids:
                        raise ValueError(f"{where}.fallbacks must not contain duplicate routes: {route_id!r}")
                    fallback_ids.add(route_id)
                    effort = route.get("reasoning_effort")
                    if effort is not None and (not isinstance(effort, str) or effort not in EFFORT_LADDER):
                        raise ValueError(f"{where}.fallbacks reasoning_effort is invalid: {effort!r}")
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{where}.{key} must be a nonempty string")
        for key in ("provider", "model", "reasoning_effort"):
            if key in fields and fields[key] != fields[key].strip():
                raise ValueError(f"{where}.{key} must not contain surrounding whitespace")
        if "provider" in fields and "model" not in fields:
            raise ValueError(f"{where}: an explicit provider requires an explicit model")
        if fields.get("inherit_parent") and any(key in fields for key in ("provider", "model", "reasoning_effort", "moa_presets")):
            raise ValueError(f"{where}.inherit_parent cannot be combined with primary route settings")
        if fields.get("provider") == "moa":
            if "reasoning_effort" in fields or "fallbacks" in fields:
                raise ValueError(f"{where}: MoA roles own effort and fallbacks in their native preset")
            if fields.get("moa_presets") is not None and fields.get("model") not in fields["moa_presets"]:
                raise ValueError(f"{where}.model must be included in moa_presets")
        elif "moa_presets" in fields:
            raise ValueError(f"{where}.moa_presets is only valid for provider: moa")
        effort = fields.get("reasoning_effort")
        if effort is not None and effort not in EFFORT_LADDER:
            raise ValueError(f"{where}.reasoning_effort is invalid: {effort!r}")
        normalized = dict(fields)
        context_mode = fields.get("context_mode", "fork" if name == "owner" else "fresh")
        if context_mode not in ("fresh", "fork"):
            raise ValueError(f"{where}.context_mode must be fresh or fork")
        normalized["context_mode"] = context_mode
        if "moa_presets" in normalized:
            normalized["moa_presets"] = tuple(normalized["moa_presets"])
        if "fallbacks" in normalized:
            normalized["fallbacks"] = tuple(FallbackDefinition(**route) for route in normalized["fallbacks"])
        definitions[name] = SubagentDefinition(name=name, **normalized)
    return definitions


def resolve_definition(
    definitions: Mapping[str, SubagentDefinition], selected: str | None,
) -> SubagentDefinition | None:
    """Omission preserves legacy behavior. Explicit invalid selection fails."""
    if selected is None:
        return None
    if not isinstance(selected, str) or not _NAME.fullmatch(selected):
        raise ValueError("subagent_type must be a valid configured identifier")
    if selected not in definitions:
        raise ValueError(f"Unknown subagent_type: {selected!r}")
    return definitions[selected]


def advertised_settings(definition: SubagentDefinition, defaults: Mapping) -> dict:
    """Nonsecret model/effort facts to show the parent in the tool schema.

    Resolved without a parent agent (schema rebuilds happen outside a turn),
    so anything that would only be known at launch is reported as inherited
    rather than guessed. Never touches credentials.
    """
    model = definition.model or (None if definition.inherit_parent else defaults.get("model") or None)
    provider = definition.provider or (None if definition.inherit_parent else defaults.get("provider") or None)
    effort = definition.reasoning_effort or (None if definition.inherit_parent else defaults.get("reasoning_effort") or None)
    return {
        "name": definition.name,
        "context_mode": definition.context_mode,
        "description": definition.description,
        "model": model or "inherits the parent model",
        "provider": provider or "inherits the parent provider",
        "reasoning_effort": effort or "inherits the parent effort",
        # A named role's settings are fixed at launch: the model chooses the
        # identifier, configuration chooses everything else.
        "pinned": bool(definition.model or definition.reasoning_effort or definition.inherit_parent),
        **({"moa_presets": list(definition.moa_presets or (definition.model,))} if definition.provider == "moa" else {}),
    }


def resolve_named_credentials(definition: SubagentDefinition, defaults: Mapping, parent):
    """Resolve without launching a child or changing the parent's credentials.

    Codex definitions deliberately reuse the parent's *current* subscription
    token, rather than consulting the global credential pool (which may select
    another account). Unavailable parent subscription auth fails closed.
    """
    from agent.reasoning_effort import (
        clamp_effort, requested_effort, transport_supported_reasoning_efforts,
    )
    from hermes_constants import parse_reasoning_effort

    provider = definition.provider or (getattr(parent, "provider", None) if definition.inherit_parent else defaults.get("provider")) or getattr(parent, "provider", None)
    model = definition.model or (getattr(parent, "model", None) if definition.inherit_parent else defaults.get("model")) or getattr(parent, "model", None)
    if provider == "moa":
        from agent.moa_loop import snapshot_moa_preset
        snapshot = snapshot_moa_preset(str(model))
        return {"provider": "moa", "model": model, "base_url": "moa://local", "api_key": None,
                "api_mode": "chat_completions", "request_overrides": None, "max_output_tokens": None,
                "moa_snapshot": snapshot}, None
    if not isinstance(model, str) or not model:
        raise ValueError(f"subagent_type {definition.name!r}: no resolved model")
    if provider == "openai-codex" and getattr(parent, "provider", None) == "openai-codex":
        base_url = getattr(parent, "base_url", "") or ""
        url = urlsplit(base_url)
        key = getattr(parent, "api_key", None) or getattr(parent, "_client_kwargs", {}).get("api_key")
        if (
            getattr(parent, "api_mode", None) != "codex_responses"
            or url.scheme != "https" or url.netloc != "chatgpt.com"
            or url.path.rstrip("/") != "/backend-api/codex"
            or url.query or url.fragment
            or not isinstance(key, str) or not key
        ):
            raise ValueError(f"subagent_type {definition.name!r}: requires the parent's authorized Codex subscription route")
        creds = {
            "provider": provider, "model": model, "base_url": base_url,
            "api_key": key, "api_mode": "codex_responses",
            "request_overrides": deepcopy(getattr(parent, "request_overrides", {}) or {}),
            "max_output_tokens": None,
        }
    elif provider == "openai-codex":
        # An explicit Codex route has its own configured OAuth authority.  It is
        # not required to match the parent provider, but it must resolve through
        # the native runtime-provider cache (never an arbitrary API-key fallback).
        from hermes_cli.runtime_provider import resolve_runtime_provider
        try:
            creds = resolve_runtime_provider(requested=provider, target_model=model)
        except Exception as exc:
            raise ValueError(
                f"subagent_type {definition.name!r}: requires an authorized configured Codex subscription route: {exc}"
            ) from exc
        if creds.get("provider") != "openai-codex" or creds.get("api_mode") != "codex_responses":
            raise ValueError(
                f"subagent_type {definition.name!r}: configured Codex route did not resolve to subscription authority"
            )
    else:
        from tools.delegate_tool import _resolve_delegation_credentials

        config = {} if definition.inherit_parent else dict(defaults)
        config.update({"model": model})
        if definition.provider:
            # An explicit provider/model replaces an unrelated default route.
            for key in ("base_url", "api_key", "api_mode", "request_overrides"):
                config.pop(key, None)
            config["provider"] = definition.provider
        creds = _resolve_delegation_credentials(config, parent)
        provider = creds.get("provider") or getattr(parent, "provider", None)
    supported = transport_supported_reasoning_efforts(
        provider, model, creds.get("api_mode") or getattr(parent, "api_mode", None),
    )

    explicit = definition.reasoning_effort
    effort = explicit
    if effort is None:
        inherited = None if definition.inherit_parent else defaults.get("reasoning_effort")
        if inherited is not None:
            parsed = parse_reasoning_effort(inherited)
        else:
            parsed = getattr(parent, "reasoning_config", None)
        effort = "none" if parsed and parsed.get("enabled") is False else requested_effort(parsed)
        # Inherit only through supported provider semantics, never invent a
        # value when the provider does not declare reasoning support.
        effort = clamp_effort(effort, supported) if supported else None
        if effort is None and provider == "openai-codex" and "medium" in (supported or ()):
            effort = "medium"
    elif not supported or effort not in supported:
        raise ValueError(f"subagent_type {definition.name!r}: reasoning_effort {effort!r} unsupported by {provider}/{model}")
    # Preflight rejection of an unpinnable route, before any child exists
    # (item 4). ``creds`` omits api_mode when the child inherits the parent's,
    # so fall back to the parent's; when neither is known yet the authoritative
    # check is RuntimePin.from_child against the constructed child, and this
    # preflight stays quiet rather than guessing.
    preflight_mode = creds.get("api_mode") or getattr(parent, "api_mode", None)
    if preflight_mode:
        unsupported = pinning_support_error(
            creds.get("provider") or provider, preflight_mode,
        )
        if unsupported:
            raise ValueError(f"subagent_type {definition.name!r}: {unsupported}")
    reasoning = parse_reasoning_effort(effort) if effort is not None else None
    return creds, reasoning


def freeze_fallback_routes(
    definition: SubagentDefinition, *, primary_provider: str, primary_model: str, parent=None,
) -> tuple[ResolvedRoute, ...]:
    """Authorize and freeze every optional route before a child can spawn."""
    if definition.inherit_parent and definition.fallbacks is None and parent is not None:
        from tools.custom_subagent_fallbacks import freeze_parent_fallback_routes
        return freeze_parent_fallback_routes(parent, primary_provider, primary_model)
    if not definition.fallbacks:
        return ()
    from hermes_cli.runtime_provider import resolve_runtime_provider

    frozen: list[ResolvedRoute] = []
    seen = {(primary_provider, primary_model)}
    for index, route in enumerate(definition.fallbacks):
        if (route.provider, route.model) in seen:
            continue
        try:
            runtime = resolve_runtime_provider(requested=route.provider, target_model=route.model)
        except Exception as exc:
            raise ValueError(
                f"subagent_type {definition.name!r}: fallback {index} cannot be authorized: {exc}"
            ) from exc
        frozen.append(_freeze_fallback_runtime(
            runtime, route, f"subagent_type {definition.name!r}: fallback {index}"
        ))
        seen.add((route.provider, route.model))
    from tools.custom_subagent_fallbacks import validate_fallback_identities
    return validate_fallback_identities(frozen, primary_provider, primary_model)


def _freeze_fallback_runtime(runtime, route, label):
    provider = str(runtime.get("provider") or route.provider).strip()
    model = str(runtime.get("model") or route.model).strip()
    base_url = str(runtime.get("base_url") or "").rstrip("/")
    api_mode = str(runtime.get("api_mode") or "").strip()
    api_key = runtime.get("api_key")
    if not provider or not model or not base_url or not api_mode or not api_key:
        raise ValueError(
            f"{label} resolved an incomplete runtime route"
        )
    unsupported = pinning_support_error(provider, api_mode)
    if unsupported:
        raise ValueError(f"{label}: {unsupported}")
    if route.reasoning_effort is not None:
        from agent.reasoning_effort import transport_supported_reasoning_efforts
        supported = transport_supported_reasoning_efforts(provider, model, api_mode)
        if not supported or route.reasoning_effort not in supported:
            raise ValueError(
                f"{label} reasoning_effort "
                f"{route.reasoning_effort!r} unsupported by {provider}/{model}"
            )
    from agent.auxiliary_client import _endpoint_default_headers
    from tools.delegate_tool_config import _merge_request_overrides
    headers = _endpoint_default_headers(base_url, provider, xai=True) or {}
    overrides = _merge_request_overrides(
        {"extra_headers": headers} if headers else {}, runtime.get("request_overrides"))
    pool = runtime.get("credential_pool")
    credential_id = None
    if pool is not None and callable(getattr(pool, "entry_id_for_api_key", None)):
        credential_id = pool.entry_id_for_api_key(str(api_key))
    return ResolvedRoute(
        provider, model, base_url, api_mode, route.reasoning_effort,
        str(api_key), hashlib.sha256(str(api_key).encode()).hexdigest(),
        json.dumps(overrides or {}, sort_keys=True,
                   separators=(",", ":"), default=str),
        pool, credential_id,
    )


def inherited_credential_pool(child, parent, defaults):
    """Inherit existing account authority, never turn a fixed route into a global pool."""
    from agent.credential_pool import credential_pool_matches_provider
    from hermes_cli.route_identity import normalize_route_base_url

    pool = getattr(parent, "_credential_pool", None)
    if pool is None or defaults.get("api_key"):
        return None
    if (
        child.provider != parent.provider
        or normalize_route_base_url(child.base_url) != normalize_route_base_url(parent.base_url)
        or not credential_pool_matches_provider(pool, child.provider, base_url=child.base_url)
        or pool.entry_id_for_api_key(child.api_key) is None
    ):
        return None
    return pool


def resolution_metadata(child):
    pin = getattr(child, "_delegation_runtime_pin", None)
    common = {
        "parent_session_id": getattr(child, "_parent_session_id", None),
        "child_session_id": getattr(child, "session_id", None),
        "unavailable_memory_providers": deepcopy(getattr(
            getattr(child, "_memory_manager", None),
            "_read_only_unavailable_providers", [],
        )),
    }
    if isinstance(pin, RuntimePin):
        child_provider = getattr(child, "provider", pin.provider)
        child_model = getattr(child, "model", pin.model)
        active = next((route for route in pin.fallback_routes
                       if (route.provider, route.model) == (child_provider, child_model)), None)
        return {
            **pin.metadata(), **common,
            "provider": child_provider, "model": child_model,
            "reasoning_effort": active.reasoning_effort if active else pin.reasoning_effort,
            "route_transitions": list(getattr(child, "_delegation_route_transitions", ()) or ()),
        }
    snapshot = getattr(child, "_moa_preset_snapshot", None)
    if snapshot is not None and callable(getattr(snapshot, "metadata", None)):
        return {
            **common, "subagent_type": getattr(child, "_delegation_named_type", None),
            "provider": "moa", "model": getattr(child, "model", None),
            "route_category": "frozen_moa", **snapshot.metadata(),
        }
    return {}


# API modes whose FINAL physical request Hermes can inspect and therefore
# actually pin: model, route, credential and (where the wire carries it)
# reasoning effort are checked immediately before the SDK call, after every
# middleware/transform has run.
#
# Anything else — bedrock_converse (boto3 Converse), the in-process ``moa``
# facade, native-Gemini transports — reaches the network through a path with
# no comparable final-boundary hook, so a pin there would be a promise we do
# not keep. Those routes are REJECTED at launch instead of silently receiving
# a weaker guarantee (item 4).
PINNABLE_API_MODES = frozenset({
    "codex_responses", "chat_completions", "anthropic_messages",
})
UNPINNABLE_PROVIDERS = frozenset({"moa", "bedrock"})


def pinning_support_error(provider: str | None, api_mode: str | None) -> str | None:
    """Return why this route cannot carry a named-subagent pin, or None."""
    if api_mode not in PINNABLE_API_MODES:
        return (
            f"api_mode {api_mode!r} has no inspectable final-request boundary, "
            f"so provider/model/effort pinning cannot be enforced. Supported: "
            f"{', '.join(sorted(PINNABLE_API_MODES))}."
        )
    if provider in UNPINNABLE_PROVIDERS:
        return (
            f"provider {provider!r} dispatches through its own client and "
            "cannot be pinned at the final request boundary."
        )
    return None


def _validate_physical_auth(client, kwargs, *, digest: str, pinned: bool, overrides: str) -> None:
    """Check SDK-merged authentication without invoking dynamic credential sources."""
    from collections.abc import Mapping
    import httpx
    from openai import Omit as OpenAIOmit
    from anthropic import Omit as AnthropicOmit

    auth_names = {"authorization", "x-api-key", "api-key", "chatgpt-account-id"}
    error = "named subagent SDK client credential changed or cannot be verified"
    http_client = getattr(client, "_client", None)
    hooks = getattr(http_client, "event_hooks", None)
    if (callable(getattr(client, "_api_key_provider", None))
            or callable(getattr(client, "_azure_ad_token_provider", None))
            or (isinstance(hooks, dict) and hooks.get("request"))
            or (isinstance(http_client, (httpx.Client, httpx.AsyncClient)) and http_client.auth is not None)):
        raise ValueError(error)

    def headers(value):
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError(error)
        return dict(value)

    omit_types = (OpenAIOmit, AnthropicOmit)

    def auth_only(values):
        selected = {}
        for name, value in values.items():
            name = str(name).lower()
            if name not in auth_names or isinstance(value, omit_types):
                continue
            if not isinstance(value, str) or (name in selected and selected[name] != value):
                raise ValueError(error)
            selected[name] = value
        return selected

    frozen = auth_only(headers(json.loads(overrides).get("extra_headers")))
    request_headers = headers(kwargs.get("extra_headers"))
    if auth_only(request_headers) != frozen:
        raise ValueError(error)
    defaults = getattr(client, "default_headers", None)
    if not isinstance(defaults, Mapping):
        defaults = {}
        key = getattr(client, "api_key", None)
        token = getattr(client, "auth_token", None)
        if isinstance(key, str) and key:
            defaults["Authorization"] = "Bearer " + key
        if isinstance(token, str) and token:
            defaults["Authorization"] = "Bearer " + token

    def authorized(name, value):
        if frozen.get(name) == value:
            return True
        credential = value[7:] if name == "authorization" and value.startswith("Bearer ") else value
        if hashlib.sha256(credential.encode()).hexdigest() == digest:
            return True
        return (not pinned and not frozen and not getattr(client, "auth_token", None)
                and name == "authorization" and value == "Bearer " + str(getattr(client, "api_key", "")))

    # Do not conceal a foreign or internally conflicting SDK default behind a
    # legitimate request override. Omit suppresses only its exact SDK key.
    default_auth = auth_only(defaults)
    if any(not authorized(name, value) for name, value in default_auth.items()):
        raise ValueError(error)

    # The SDK merges dicts case-sensitively before constructing httpx.Headers.
    # Normalize the ACTUAL request override, suppressing every shadowed default
    # spelling so both the verifier and wire see one authorized channel.
    normalized = {key: value for key, value in request_headers.items() if str(key).lower() not in auth_names}
    requested_names = {str(key).lower() for key in request_headers if str(key).lower() in auth_names}
    omit = AnthropicOmit if type(client).__module__.startswith("anthropic") else OpenAIOmit
    for name in requested_names:
        supplied = [value for key, value in request_headers.items() if str(key).lower() == name]
        if any(isinstance(value, omit_types) for value in supplied) and not all(isinstance(value, omit_types) for value in supplied):
            raise ValueError(error)
        default_keys = [key for key in defaults if str(key).lower() == name]
        key = default_keys[0] if default_keys else next(key for key in request_headers if str(key).lower() == name)
        for shadow in default_keys:
            normalized[shadow] = omit()
        normalized[key] = omit() if isinstance(supplied[0], omit_types) else supplied[0]
    effective = auth_only({**defaults, **normalized})
    if (any(effective.get(name) != value for name, value in frozen.items())
            or (pinned and not effective)
            or any(not authorized(name, value) for name, value in effective.items())):
        raise ValueError(error)
    if requested_names:
        kwargs["extra_headers"] = normalized


@dataclass(frozen=True, slots=True)
class RuntimePin:
    """Nonsecret launch configuration retained for the child's full lifetime."""
    subagent_type: str
    provider: str
    model: str
    base_url: str
    api_mode: str
    reasoning_effort: str | None
    _credential_digest: str = field(repr=False)
    # Whether the child launched WITH a credential at all. Distinguishes "the
    # credential changed" from "this route never had one", which a digest of
    # the empty string cannot express.
    _pinned_credential: bool = field(default=False, repr=False)
    _credential_pool: CredentialPool | None = field(default=None, repr=False, compare=False)
    fallback_routes: tuple[ResolvedRoute, ...] = ()
    request_overrides_json: str = "{}"

    @classmethod
    def from_child(cls, child, definition, reasoning):
        from agent.reasoning_effort import requested_effort
        unsupported = pinning_support_error(child.provider, child.api_mode)
        if unsupported:
            raise ValueError(f"subagent_type {definition.name!r}: {unsupported}")
        effort = "none" if reasoning and reasoning.get("enabled") is False else requested_effort(reasoning)
        digest = hashlib.sha256(str(child.api_key or "").encode()).hexdigest()
        fallbacks = tuple(getattr(child, "_delegation_fallback_routes", ()))
        request_overrides = json.dumps(
            getattr(child, "request_overrides", {}) or {}, sort_keys=True,
            separators=(",", ":"), default=str,
        )
        return cls(definition.name, child.provider, child.model, child.base_url,
                   child.api_mode, effort, digest,
                   bool(isinstance(child.api_key, str) and child.api_key),
                   getattr(child, "_credential_pool", None), fallbacks,
                   request_overrides)

    def for_pool_swap(self, child, entry, api_key, base_url):
        """Advance only the active route's credential at its frozen pool boundary."""
        from agent.credential_pool import credential_pool_matches_provider
        from hermes_cli.route_identity import normalize_route_base_url

        current = (child.provider, child.model, child.base_url, child.api_mode)
        primary = (self.provider, self.model, self.base_url, self.api_mode)
        fallback = next((route for route in self.fallback_routes if _same_pinned_route(
            current, route.provider, route.model, route.base_url, route.api_mode
        )), None)
        pool = self._credential_pool if _same_pinned_route(current, *primary) else (
            fallback._credential_pool if fallback is not None else None
        )
        expected_provider = self.provider if fallback is None else fallback.provider
        expected_base_url = self.base_url if fallback is None else fallback.base_url
        if (
            pool is None or getattr(child, "_credential_pool", None) is not pool
            or not credential_pool_matches_provider(
                pool, expected_provider, base_url=expected_base_url
            )
            or entry.provider != pool.provider
            or not any(candidate is entry for candidate in pool.entries())
            or normalize_route_base_url(base_url) != normalize_route_base_url(expected_base_url)
            or (not _same_pinned_route(current, *primary) and fallback is None)
        ):
            raise ValueError(f"subagent_type {self.subagent_type!r}: unauthorized credential rotation")
        digest = hashlib.sha256(api_key.encode()).hexdigest()
        if fallback is None:
            return replace(self, _credential_digest=digest, _pinned_credential=bool(api_key))
        rotated = replace(
            fallback, api_key=api_key, credential_digest=digest,
            credential_pool_entry_id=getattr(entry, "id", None),
        )
        return replace(self, fallback_routes=tuple(
            rotated if route is fallback else route for route in self.fallback_routes
        ))

    def pinned_base_url_for(self, child) -> str:
        """Return the exact frozen spelling for the child's active route."""
        current = (child.provider, child.model, child.base_url, child.api_mode)
        if _same_pinned_route(current, self.provider, self.model, self.base_url, self.api_mode):
            return self.base_url
        fallback = next((route for route in self.fallback_routes if _same_pinned_route(
            current, route.provider, route.model, route.base_url, route.api_mode
        )), None)
        if fallback is None:
            raise ValueError(f"subagent_type {self.subagent_type!r}: pinned route changed")
        return fallback.base_url

    def validate_request(self, child, kwargs, *, client=None, final_request=None):
        """Assert the pinned route/model/effort/credential for one request.

        Called twice by design: once while kwargs are built, and once at the
        final boundary with the client that will actually send them. The second
        call is the one that matters — middleware, fallback chains and client
        replacement all run in between (item 4).
        """
        if final_request is None:
            final_request = client is not None
        current = (child.provider, child.model, child.base_url, child.api_mode)
        expected = (self.provider, self.model, self.base_url, self.api_mode)
        digest = hashlib.sha256(str(child.api_key or "").encode()).hexdigest()
        fallback = next((route for route in self.fallback_routes
                         if (route.provider, route.model) == current[:2]), None)
        if not _same_pinned_route(current, *expected) and (
            fallback is None or not _same_pinned_route(
                current, fallback.provider, fallback.model, fallback.base_url, fallback.api_mode
            )
        ):
            raise ValueError(f"subagent_type {self.subagent_type!r}: pinned route changed")
        expected_digest = self._credential_digest if fallback is None else fallback.credential_digest
        if digest != expected_digest:
            raise ValueError(f"subagent_type {self.subagent_type!r}: pinned route changed")
        actual_request_overrides = getattr(child, "request_overrides", {}) or {}
        active_provider = fallback.provider if fallback else self.provider
        override_headers = (
            actual_request_overrides.get("extra_headers", {})
            if isinstance(actual_request_overrides, dict) else {}
        )
        if active_provider == "openai-codex" and any(
            str(key).lower() in _AUTH_HEADER_NAMES for key in override_headers
        ):
            raise ValueError("Named Codex subagents cannot override authentication headers")
        expected_overrides = self.request_overrides_json if fallback is None else fallback.request_overrides_json
        actual_overrides = json.dumps(
            actual_request_overrides, sort_keys=True,
            separators=(",", ":"), default=str,
        )
        if actual_overrides != expected_overrides:
            raise ValueError(f"subagent_type {self.subagent_type!r}: pinned request overrides changed")
        extra = kwargs.get("extra_body") or {}
        active_model = current[1]
        if kwargs.get("model") != active_model or extra.get("model", active_model) != active_model:
            raise ValueError(f"subagent_type {self.subagent_type!r}: pinned request model changed")
        active_effort = fallback.reasoning_effort if fallback else self.reasoning_effort
        if active_effort is not None:
            self._validate_request_effort(
                kwargs, extra, active_effort,
                api_mode=fallback.api_mode if fallback else self.api_mode,
            )
        if (fallback.provider if fallback else self.provider) == "openai-codex":
            # Codex subscription auth rides in headers, so a per-request header
            # override IS a credential swap. Other wires carry credentials on
            # the client, which _validate_client checks instead.
            headers = kwargs.get("extra_headers") or {}
            if any(str(key).lower() in _AUTH_HEADER_NAMES for key in headers):
                raise ValueError("Named Codex subagents cannot override authentication headers")
        if client is None:
            client = getattr(child, "client", None)
        if client is not None:
            if active_provider == "openai-codex":
                active_pin = self if fallback is None else replace(
                    self, provider=fallback.provider, base_url=fallback.base_url,
                    _credential_digest=fallback.credential_digest,
                )
                active_pin._validate_client(client)
            else:
                if fallback is None:
                    self._validate_client_route(client)
                else:
                    if getattr(client, "base_url", None) is None:
                        raise ValueError("named subagent fallback client route is unavailable")
                    replace(self, provider=fallback.provider, base_url=fallback.base_url,
                            api_mode=fallback.api_mode)._validate_client_route(client)
                auth_kwargs = kwargs if final_request else json.loads(expected_overrides)
                _validate_physical_auth(
                    client, auth_kwargs, digest=expected_digest,
                    pinned=self._pinned_credential if fallback is None else bool(fallback.api_key),
                    overrides=expected_overrides,
                )

    def _validate_request_effort(self, kwargs, extra, expected_effort=None, api_mode=None) -> None:
        """Compare effort only where the request actually states one.

        Codex always carries ``reasoning.effort``, so an absent or different
        value there IS a change and is rejected.

        Every other wire is checked only when the request carries an effort
        value to compare — read from ``reasoning.effort``, ``extra_body``, or a
        top-level ``reasoning_effort``, since providers spell it differently.
        A payload that expresses reasoning some other way (a token budget, an
        exclude flag) states no effort at all, and treating that silence as a
        contradiction would abort every request the child makes from the final
        dispatch boundary — a hard outage, not a caught tampering event. The
        route, model, and credential remain pinned in all cases.
        """
        if (api_mode or self.api_mode) == "codex_responses":
            reasoning = extra.get("reasoning", kwargs.get("reasoning")) or {}
            if reasoning.get("effort") != (expected_effort or self.reasoning_effort):
                raise ValueError(
                    f"subagent_type {self.subagent_type!r}: pinned request reasoning changed"
                )
            return
        stated = self._stated_effort(kwargs, extra)
        if stated is not None and stated != (expected_effort or self.reasoning_effort):
            raise ValueError(
                f"subagent_type {self.subagent_type!r}: pinned request reasoning changed"
            )

    @staticmethod
    def _stated_effort(kwargs, extra) -> str | None:
        """The effort this request actually names, in any known spelling."""
        for source in (extra, kwargs):
            reasoning = source.get("reasoning")
            if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
                return reasoning["effort"]
            effort = source.get("reasoning_effort")
            if isinstance(effort, str) and effort:
                return effort
        return None

    def _validate_client(self, client) -> None:
        """Check the client that will physically send this request.

        Codex is strict, exactly as before this check was generalized: one
        fixed subscription endpoint, exact base_url string, and the credential
        digest compared unconditionally — a substituted client with a falsy
        api_key is a swap, not an exemption.

        Other wires are checked at the granularity that is actually stable.
        SDKs normalize base_url spelling (the Anthropic client keeps the host
        root where the config carried a ``/v1`` suffix), and this guard raises
        mid-run, so a spelling difference would be a hard outage rather than a
        caught attack. Host identity is what a real route change alters.
        """
        self._validate_client_route(client)
        api_key = getattr(client, "api_key", None)
        if self.provider == "openai-codex":
            self._compare_credential(str(api_key or ""))
            return
        _validate_physical_auth(
            client, json.loads(self.request_overrides_json), digest=self._credential_digest,
            pinned=self._pinned_credential, overrides=self.request_overrides_json,
        )

    def _validate_client_route(self, client) -> None:
        base_url = getattr(client, "base_url", None)
        if base_url is None:
            return
        if self.provider == "openai-codex":
            changed = str(base_url).rstrip("/") != self.base_url.rstrip("/")
        elif not self.base_url:
            # Nothing was pinned to compare against (provider-default route).
            return
        else:
            expected_base = self.base_url
            if self.api_mode == "anthropic_messages":
                from agent.anthropic_adapter import _base_client_kwargs
                expected_base, _ = _base_client_kwargs(expected_base, None)
            changed = not _same_pinned_base_url(str(base_url), expected_base)
        if changed:
            raise ValueError(
                f"subagent_type {self.subagent_type!r}: SDK client route changed after launch"
            )

    def _compare_credential(self, api_key: str) -> None:
        if hashlib.sha256(api_key.encode()).hexdigest() != self._credential_digest:
            raise ValueError(
                f"subagent_type {self.subagent_type!r}: SDK client credential changed after launch"
            )

    def reasoning_config(self):
        from hermes_constants import parse_reasoning_effort
        return parse_reasoning_effort(self.reasoning_effort) if self.reasoning_effort is not None else None

    def metadata(self):
        return {
            "subagent_type": self.subagent_type,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "authority_fingerprint": self._credential_digest,
            "route_category": "codex_subscription" if self.provider == "openai-codex" else "pinned_provider",
        }
