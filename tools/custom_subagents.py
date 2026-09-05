"""Trusted named delegation definitions, independent of child execution.

Only configuration selects model/provider/effort. Model-facing calls select an
identifier, never credentials or arbitrary runtime settings. Parsed definitions
are immutable snapshots, so editing configuration cannot alter a running child.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from copy import deepcopy
from urllib.parse import urlsplit
import re
from typing import Mapping

from agent.reasoning_effort import EFFORT_LADDER


@dataclass(frozen=True)
class SubagentDefinition:
    name: str
    description: str
    instructions: str
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None


_FIELDS = frozenset({"description", "instructions", "provider", "model", "reasoning_effort"})
_NAME = re.compile(r"[a-z][a-z0-9_-]*\Z")


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
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{where}.{key} must be a nonempty string")
        for key in ("provider", "model", "reasoning_effort"):
            if key in fields and fields[key] != fields[key].strip():
                raise ValueError(f"{where}.{key} must not contain surrounding whitespace")
        if "provider" in fields and "model" not in fields:
            raise ValueError(f"{where}: an explicit provider requires an explicit model")
        effort = fields.get("reasoning_effort")
        if effort is not None and effort not in EFFORT_LADDER:
            raise ValueError(f"{where}.reasoning_effort is invalid: {effort!r}")
        definitions[name] = SubagentDefinition(name=name, **fields)
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


def resolve_named_credentials(definition: SubagentDefinition, defaults: Mapping, parent):
    """Resolve without launching a child or changing the parent's credentials.

    Codex definitions deliberately reuse the parent's *current* subscription
    token, rather than consulting the global credential pool (which may select
    another account). Unavailable parent subscription auth fails closed.
    """
    from agent.reasoning_effort import codex_supported_efforts, requested_effort, clamp_effort
    from hermes_constants import parse_reasoning_effort

    provider = definition.provider or defaults.get("provider") or getattr(parent, "provider", None)
    model = definition.model or defaults.get("model") or getattr(parent, "model", None)
    if not isinstance(model, str) or not model:
        raise ValueError(f"subagent_type {definition.name!r}: no resolved model")
    if provider == "openai-codex":
        base_url = getattr(parent, "base_url", "") or ""
        url = urlsplit(base_url)
        key = getattr(parent, "api_key", None) or getattr(parent, "_client_kwargs", {}).get("api_key")
        if (
            getattr(parent, "provider", None) != "openai-codex"
            or getattr(parent, "api_mode", None) != "codex_responses"
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
        supported = codex_supported_efforts(model)
    else:
        from tools.delegate_tool import _resolve_delegation_credentials
        from providers import get_provider_profile

        config = dict(defaults)
        config.update({"model": model})
        if definition.provider:
            # An explicit provider/model replaces an unrelated default route.
            for key in ("base_url", "api_key", "api_mode", "request_overrides"):
                config.pop(key, None)
            config["provider"] = definition.provider
        creds = _resolve_delegation_credentials(config, parent)
        provider = creds.get("provider") or getattr(parent, "provider", None)
        profile = get_provider_profile(provider) if isinstance(provider, str) else None
        supported = profile.supported_reasoning_efforts(model) if profile else None

    explicit = definition.reasoning_effort
    effort = explicit
    if effort is None:
        inherited = defaults.get("reasoning_effort")
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
    reasoning = parse_reasoning_effort(effort) if effort is not None else None
    return creds, reasoning


def resolution_metadata(child):
    pin = getattr(child, "_delegation_runtime_pin", None)
    if not isinstance(pin, RuntimePin):
        return {}
    return {
        **pin.metadata(),
        "parent_session_id": getattr(child, "_parent_session_id", None),
        "child_session_id": getattr(child, "session_id", None),
        "unavailable_memory_providers": deepcopy(getattr(
            getattr(child, "_memory_manager", None),
            "_read_only_unavailable_providers", [],
        )),
    }


@dataclass(frozen=True)
class RuntimePin:
    """Nonsecret launch configuration retained for the child's full lifetime."""
    subagent_type: str
    provider: str
    model: str
    base_url: str
    api_mode: str
    reasoning_effort: str | None
    _credential_digest: str = field(repr=False)

    @classmethod
    def from_child(cls, child, definition, reasoning):
        from agent.reasoning_effort import requested_effort
        effort = "none" if reasoning and reasoning.get("enabled") is False else requested_effort(reasoning)
        digest = hashlib.sha256(str(child.api_key or "").encode()).hexdigest()
        return cls(definition.name, child.provider, child.model, child.base_url,
                   child.api_mode, effort, digest)

    def validate_request(self, child, kwargs, *, client=None):
        current = (child.provider, child.model, child.base_url, child.api_mode)
        expected = (self.provider, self.model, self.base_url, self.api_mode)
        digest = hashlib.sha256(str(child.api_key or "").encode()).hexdigest()
        if current != expected or digest != self._credential_digest:
            raise ValueError(f"subagent_type {self.subagent_type!r}: pinned route changed")
        extra = kwargs.get("extra_body") or {}
        if kwargs.get("model") != self.model or extra.get("model", self.model) != self.model:
            raise ValueError(f"subagent_type {self.subagent_type!r}: pinned request model changed")
        if self.api_mode == "codex_responses" and self.reasoning_effort is not None:
            reasoning = extra.get("reasoning", kwargs.get("reasoning")) or {}
            if reasoning.get("effort") != self.reasoning_effort:
                raise ValueError(f"subagent_type {self.subagent_type!r}: pinned request reasoning changed")
        if self.provider == "openai-codex":
            headers = kwargs.get("extra_headers") or {}
            if any(str(key).lower() in {"authorization", "chatgpt-account-id"} for key in headers):
                raise ValueError("Named Codex subagents cannot override authentication headers")
            client = client if client is not None else child.client
            client_digest = hashlib.sha256(str(client.api_key or "").encode()).hexdigest()
            if (str(client.base_url).rstrip("/") != self.base_url.rstrip("/")
                    or client_digest != self._credential_digest):
                raise ValueError("Named subagent SDK client route changed after launch")

    def reasoning_config(self):
        from hermes_constants import parse_reasoning_effort
        return parse_reasoning_effort(self.reasoning_effort) if self.reasoning_effort is not None else None

    def metadata(self):
        return {
            "subagent_type": self.subagent_type,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "route_category": "codex_subscription" if self.provider == "openai-codex" else "pinned_provider",
        }
