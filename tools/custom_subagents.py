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


def advertised_settings(definition: SubagentDefinition, defaults: Mapping) -> dict:
    """Nonsecret model/effort facts to show the parent in the tool schema.

    Resolved without a parent agent (schema rebuilds happen outside a turn),
    so anything that would only be known at launch is reported as inherited
    rather than guessed. Never touches credentials.
    """
    model = definition.model or (defaults.get("model") or None)
    provider = definition.provider or (defaults.get("provider") or None)
    effort = definition.reasoning_effort or (defaults.get("reasoning_effort") or None)
    return {
        "name": definition.name,
        "description": definition.description,
        "model": model or "inherits the parent model",
        "provider": provider or "inherits the parent provider",
        "reasoning_effort": effort or "inherits the parent effort",
        # A named role's settings are fixed at launch: the model chooses the
        # identifier, configuration chooses everything else.
        "pinned": bool(definition.model or definition.reasoning_effort),
    }


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
    # Whether the child launched WITH a credential at all. Distinguishes "the
    # credential changed" from "this route never had one", which a digest of
    # the empty string cannot express.
    _pinned_credential: bool = field(default=False, repr=False)

    @classmethod
    def from_child(cls, child, definition, reasoning):
        from agent.reasoning_effort import requested_effort
        unsupported = pinning_support_error(child.provider, child.api_mode)
        if unsupported:
            raise ValueError(f"subagent_type {definition.name!r}: {unsupported}")
        effort = "none" if reasoning and reasoning.get("enabled") is False else requested_effort(reasoning)
        digest = hashlib.sha256(str(child.api_key or "").encode()).hexdigest()
        return cls(definition.name, child.provider, child.model, child.base_url,
                   child.api_mode, effort, digest,
                   bool(isinstance(child.api_key, str) and child.api_key))

    def validate_request(self, child, kwargs, *, client=None):
        """Assert the pinned route/model/effort/credential for one request.

        Called twice by design: once while kwargs are built, and once at the
        final boundary with the client that will actually send them. The second
        call is the one that matters — middleware, fallback chains and client
        replacement all run in between (item 4).
        """
        current = (child.provider, child.model, child.base_url, child.api_mode)
        expected = (self.provider, self.model, self.base_url, self.api_mode)
        digest = hashlib.sha256(str(child.api_key or "").encode()).hexdigest()
        if current != expected or digest != self._credential_digest:
            raise ValueError(f"subagent_type {self.subagent_type!r}: pinned route changed")
        extra = kwargs.get("extra_body") or {}
        if kwargs.get("model") != self.model or extra.get("model", self.model) != self.model:
            raise ValueError(f"subagent_type {self.subagent_type!r}: pinned request model changed")
        if self.reasoning_effort is not None:
            self._validate_request_effort(kwargs, extra)
        if self.provider == "openai-codex":
            # Codex subscription auth rides in headers, so a per-request header
            # override IS a credential swap. Other wires carry credentials on
            # the client, which _validate_client checks instead.
            headers = kwargs.get("extra_headers") or {}
            if any(str(key).lower() in _AUTH_HEADER_NAMES for key in headers):
                raise ValueError("Named Codex subagents cannot override authentication headers")
        if client is None:
            client = getattr(child, "client", None)
        if client is not None:
            self._validate_client(client)

    def _validate_request_effort(self, kwargs, extra) -> None:
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
        if self.api_mode == "codex_responses":
            reasoning = extra.get("reasoning", kwargs.get("reasoning")) or {}
            if reasoning.get("effort") != self.reasoning_effort:
                raise ValueError(
                    f"subagent_type {self.subagent_type!r}: pinned request reasoning changed"
                )
            return
        stated = self._stated_effort(kwargs, extra)
        if stated is not None and stated != self.reasoning_effort:
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
        if not isinstance(api_key, str) or not api_key or not self._pinned_credential:
            # Nothing meaningful to compare: a header-auth transport, or a
            # keyless provider whose SDK supplies a placeholder. Comparing that
            # against the digest of "" would kill every request on those
            # routes. The route check above still holds.
            return
        self._compare_credential(api_key)

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
            changed = _route_host(str(base_url)) != _route_host(self.base_url)
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
            "route_category": "codex_subscription" if self.provider == "openai-codex" else "pinned_provider",
        }
