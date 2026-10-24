"""Explicit live references to the existing model_aliases registry.

Bare aliases retain upstream copy-on-pick semantics. ``alias:name`` is stored
verbatim and dereferenced only at a turn/fire/spawn boundary, never in a child
that has already launched. No process-global alias cache participates here.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeGuard


PREFIX = "alias:"
_SNAPSHOT: ContextVar[dict[str, Any] | None] = ContextVar("live_alias_snapshot", default=None)


def is_live_alias(model: Any) -> TypeGuard[str]:
    return isinstance(model, str) and model.strip().startswith(PREFIX)


def current_config() -> dict:
    snapshot = _SNAPSHOT.get()
    if snapshot is not None and "config" in snapshot:
        return snapshot["config"]
    from hermes_cli.config import get_config_path
    from hermes_cli.config_effective import load_user_config_effective
    config = load_user_config_effective(get_config_path())
    if snapshot is not None:
        snapshot["config"] = config
    return config


@contextmanager
def live_alias_scope():
    """Lazily hold one alias snapshot for a coupled selection and resolution."""
    token = _SNAPSHOT.set({})
    try:
        yield
    finally:
        _SNAPSHOT.reset(token)


def with_live_alias_snapshot(function):
    @wraps(function)
    def scoped(*args, **kwargs):
        with live_alias_scope():
            return function(*args, **kwargs)
    return scoped


@dataclass(frozen=True)
class LiveAlias:
    reference: str
    model: str
    provider: str
    base_url: str
    api_key: Any
    reasoning_config: dict | None
    config: dict

    def runtime(self) -> dict:
        from hermes_cli.model_switch import direct_alias_runtime_request
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from hermes_cli.model_switch import DirectAlias
        requested, key = direct_alias_runtime_request(
            DirectAlias(self.model, self.provider, self.base_url, self.api_key))
        return resolve_runtime_provider(
            requested=requested, explicit_base_url=self.base_url or None,
            explicit_api_key=key, target_model=self.model)


def resolve_live_alias(reference: str, config: dict | None = None) -> LiveAlias:
    """Resolve one strict alias from ONE current snapshot, failing closed on a typo/deletion."""
    from hermes_cli.model_switch import _load_direct_aliases
    from hermes_constants import parse_reasoning_effort, resolve_reasoning_config
    if not is_live_alias(reference):
        raise ValueError(f"Not a live model alias: {reference!r}")
    name = reference.strip()[len(PREFIX):].strip().lower()
    cfg = current_config() if config is None else config
    authored = cfg.get("model_aliases") or {}
    entries = {str(key).lower(): value for key, value in authored.items()} if isinstance(authored, dict) else {}
    if not name or name not in entries:
        raise ValueError(f"Unknown live model alias {reference!r} in model_aliases")
    route = _load_direct_aliases(cfg).get(name)
    if not route or not route.model or is_live_alias(route.model):
        raise ValueError(f"Invalid live model alias {reference!r}: expected a concrete model")
    from hermes_cli.model_switch import direct_alias_api_key
    model, provider, base_url = route.model, route.provider, route.base_url
    api_key = direct_alias_api_key(route)
    definition = entries[name]
    if isinstance(definition, dict) and "reasoning_effort" in definition:
        reasoning = parse_reasoning_effort(definition["reasoning_effort"])
        if reasoning is None:
            raise ValueError(f"Invalid reasoning_effort for live model alias {reference!r}")
    else:
        reasoning = resolve_reasoning_config(cfg, model)
    return LiveAlias(reference.strip(), model, provider, base_url or "", api_key or "", reasoning, cfg)
