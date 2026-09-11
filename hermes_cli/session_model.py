"""Turn-bound session model controls for optional plugin tools.

Frontends bind their existing switch applier around a conversation. Tools only
prepare a request. The owning frontend commits it after the whole turn finishes.
No process-global session lookup, synthetic slash messages, or config writes.
"""
from __future__ import annotations

import copy
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

_CURRENT: ContextVar[SessionModelControl | None] = ContextVar("session_model_control", default=None)


@dataclass
class Selection:
    route: Any
    reasoning: dict | None
    baseline: tuple
    warnings: list[str]


def _identity(agent) -> tuple:
    return (agent.model, agent.provider, agent.base_url, agent.api_mode,
            json.dumps(getattr(agent, "reasoning_config", None), sort_keys=True))


def _level(config) -> str:
    if config is None:
        return "default"
    return "none" if config.get("enabled") is False else config.get("effort", "default")


def _validate_reasoning(route, config) -> None:
    """Reject known-incompatible effort; unknown routes require explicit native setup.

    Use provider declarations and transport policy, not a second model catalog.
    This tool is stricter than slash commands, which may silently clamp efforts.
    """
    if config is None:
        return
    from agent.reasoning_effort import transport_supported_reasoning_efforts
    from utils import base_url_host_matches

    level = _level(config)
    supported = transport_supported_reasoning_efforts(
        route.target_provider, route.new_model, route.api_mode,
    )
    mandatory = False
    if supported is None and base_url_host_matches(route.base_url, "openrouter.ai"):
        from hermes_cli.models_reasoning_caps import openrouter_model_reasoning_capabilities
        caps = openrouter_model_reasoning_capabilities(route.new_model)
        if caps is not None:
            supported = caps.get("supported_efforts") if caps.get("supports_reasoning") else ()
            mandatory = caps.get("mandatory", False)
    if mandatory and level == "none":
        raise ValueError("The target model requires thinking and cannot disable it.")
    if supported is None:
        raise ValueError("Cannot verify this route's reasoning levels. Use the native model/reasoning controls for this route.")
    if level not in supported:
        raise ValueError(f"Reasoning {level!r} is unsupported for {route.new_model}. Supported: {', '.join(supported) or 'none (no controls)'}. Specify a compatible level explicitly.")


class SessionModelControl:
    def __init__(self, agent, apply: Callable[[Selection], None], *, allowed: bool = True, history=None):
        self.agent = agent
        self.apply = apply
        self.allowed = allowed
        self.history = history or []
        self.pending: Selection | None = None
        self.lock = threading.Lock()
        self.closed = False

    def request(self, args: dict, task_id: str | None) -> dict:
        with self.lock:
            if self.closed or not self.allowed or task_id != self.agent.session_id:
                raise ValueError("Session model controls are unavailable for this turn or caller.")
            if self.pending is not None:
                raise ValueError("A session model change is already queued for this turn.")
            if not args or set(args) - {"model", "provider", "reasoning"}:
                raise ValueError("Provide model, provider, and/or reasoning only.")
            if any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in args.values()):
                raise ValueError("Settings must be nonempty strings without surrounding whitespace.")
            if "provider" in args and "model" not in args:
                raise ValueError("A provider change requires an explicit model.")
            if any(any(c.isspace() for c in args[k]) or args[k].startswith("-") for k in ("model", "provider") if k in args):
                raise ValueError("Use a model ID or alias and provider ID, not command flags.")
            from hermes_constants import parse_reasoning_effort
            from hermes_cli.config import load_config, get_compatible_custom_providers
            from hermes_cli.model_switch import ModelSwitchResult, switch_model
            from hermes_cli.model_selection_guards import combined_selection_warning

            reasoning = copy.deepcopy(getattr(self.agent, "reasoning_config", None))
            if "reasoning" in args:
                reasoning = parse_reasoning_effort(args["reasoning"])
                if reasoning is None:
                    raise ValueError("Unknown reasoning effort. Display controls are not supported here.")
            baseline = _identity(self.agent)
            cfg = load_config() or {}
            custom = get_compatible_custom_providers(cfg)
            if "model" in args:
                route = switch_model(
                    raw_input=args["model"], explicit_provider=args.get("provider", ""),
                    current_model=self.agent.model, current_provider=self.agent.provider,
                    current_base_url=self.agent.base_url, current_api_key=self.agent.api_key,
                    is_global=False, user_providers=cfg.get("providers") or {}, custom_providers=custom,
                )
                if not route.success:
                    raise ValueError(route.error_message)
                guard = combined_selection_warning(route.new_model, provider=route.target_provider,
                    base_url=route.base_url, api_key=route.api_key, model_info=route.model_info)
                if guard:
                    raise ValueError(f"Native confirmation required: {guard.title}. Use /model for this selection.")
            else:
                route = ModelSwitchResult(success=True, new_model=self.agent.model,
                    target_provider=self.agent.provider, base_url=self.agent.base_url,
                    api_mode=self.agent.api_mode)
            _validate_reasoning(route, reasoning)
            warnings = []
            if "model" in args:
                if route.warning_message:
                    raise ValueError(f"{route.warning_message} Use /model to confirm this route.")
                from hermes_cli.context_switch_guard import merge_preflight_compression_warning
                merge_preflight_compression_warning(route, agent=self.agent,
                    messages=list(self.history),
                    custom_providers=custom,
                    config_context_length=getattr(self.agent, "_config_context_length", None))
                if route.warning_message:
                    warnings.append(route.warning_message)
                warnings.append("The new model starts with a cold prompt cache; normal context preflight/compression applies next turn.")
            self.pending = Selection(route if "model" in args else None, reasoning, baseline, warnings)
            return {"success": True, "status": "queued", "scope": "session",
                    "model": route.new_model, "provider": route.target_provider,
                    "reasoning": _level(reasoning), "warnings": warnings,
                    "message": "Not switched yet. Applies after this turn completes successfully. Do not claim it is active before the runtime receipt."}

    def finish(self, result: dict) -> dict:
        with self.lock:
            self.closed = True
            selected, self.pending = self.pending, None
        if selected is None:
            return result
        status = "cancelled"
        message = "Session model change cancelled because the turn did not complete."
        if result.get("completed") and not result.get("interrupted") and not result.get("failed"):
            try:
                if _identity(self.agent) != selected.baseline:
                    raise ValueError("Session settings changed while the request was queued. Retry against the current settings.")
                self.apply(selected)
                status = "applied"
                message = f"Session settings applied: {self.agent.model} via {self.agent.provider}, reasoning {_level(selected.reasoning)}. Effective next turn."
            except Exception as exc:
                from agent.redact import redact_sensitive_text
                status = "failed"
                message = "Session model change failed: " + redact_sensitive_text(str(exc))
        result["session_model"] = {"status": status, "message": message}
        from agent.turn_finalizer import synchronize_terminal_response
        synchronize_terminal_response(
            self.agent, result, (result.get("final_response") or "") + "\n\n" + message,
        )
        return result


@contextmanager
def session_model_scope(agent, apply, *, allowed=True, history=None):
    control = SessionModelControl(agent, apply, allowed=allowed, history=history)
    token = _CURRENT.set(control)
    try:
        yield control
    finally:
        control.closed = True
        control.pending = None
        _CURRENT.reset(token)


def request_session_model(args: dict, *, task_id=None) -> str:
    """Plugin-facing entrypoint, bound only during a supported frontend's turn."""
    try:
        control = _CURRENT.get()
        if control is None:
            raise ValueError("Session model controls are unavailable on this surface.")
        return json.dumps(control.request(args, task_id))
    except Exception as exc:
        from agent.redact import redact_sensitive_text
        return json.dumps({"success": False, "status": "rejected", "error": redact_sensitive_text(str(exc))})
