"""Request attribution for explicitly opted-in CLIProxyAPI routes."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx

HEADER = "X-Hermes-Request-Class"
_HTTP_MODES = {"chat_completions", "codex_responses", "anthropic_messages"}
logger = logging.getLogger(__name__)
_BACKGROUND_ORIGINS = ("cron", "relay", "process", "goal_loop", "orchestrator", "leaf", "review")
_BACKGROUND_TASKS = ("compression", "goal_judge", "review", "title_generation")
_AUX_REQUEST_CLASS: ContextVar[str | None] = ContextVar("aux_request_class", default=None)


def _class_for_origin(origin: str | None) -> str:
    return "background" if origin in _BACKGROUND_ORIGINS else "interactive"


@contextmanager
def bind_aux_request_class(request_class: str):
    token = _AUX_REQUEST_CLASS.set(request_class)
    try:
        yield
    finally:
        _AUX_REQUEST_CLASS.reset(token)


def bind_aux_request_origin(origin: str):
    """Bind a critical-path auxiliary call before the main turn starts."""
    return bind_aux_request_class(_class_for_origin(origin))


def turn_request_class(agent: Any) -> str:
    """Known autonomous work may be paced; unknown provenance must not be."""
    fork = getattr(agent, "_turn_origin", None)
    if fork == "side_question":
        return "interactive"
    if fork == "background_review" or getattr(agent, "is_subagent", False) or getattr(agent, "_delegate_depth", 0):
        return "background"
    return _class_for_origin(getattr(agent, "_turn_provenance", None))


def _strip_on_redirect(request: httpx.Request) -> None:
    if HEADER not in request.headers:
        return
    # httpx carries extensions across redirects, but builds fresh headers for each hop.
    # A proxy's HTTP redirect must not forward this header to a different URL.
    original = request.extensions.setdefault("hermes_request_class_url", request.url)
    if request.url != original:
        del request.headers[HEADER]


async def _async_strip_on_redirect(request: httpx.Request) -> None:
    _strip_on_redirect(request)


def protect_request_class_redirects(sdk_client: Any) -> None:
    """Keep HTTP redirect behavior intact while containing the proxy-only header."""
    client = getattr(sdk_client, "_client", None)
    if client is None:
        return
    if isinstance(client, httpx.AsyncClient):
        hooks = client.event_hooks["request"]
        if _async_strip_on_redirect not in hooks:
            hooks.append(_async_strip_on_redirect)
    elif isinstance(client, httpx.Client):
        hooks = client.event_hooks["request"]
        if _strip_on_redirect not in hooks:
            hooks.append(_strip_on_redirect)


def _enabled_for_route(provider: str, base_url: str) -> bool:
    # Do not infer a proxy from the host, model, or another provider at the same URL.
    if provider in {"", "auto", "custom"} or not base_url.startswith(("https://", "http://")):
        return False
    try:
        from hermes_cli.runtime_provider import load_runtime_config_readonly
        from hermes_cli.runtime_provider_custom import _get_named_custom_provider, _route_key
        from utils import base_url_hostname

        if not base_url_hostname(base_url):
            return False
        entry = _get_named_custom_provider(
            provider, config=load_runtime_config_readonly(), resolve_credentials=False,
        )
        return bool(entry and entry.get("hermes_request_class") is True
                    and _route_key(entry.get("base_url")) == _route_key(base_url))
    except Exception:
        # A scheduling hint must not turn an otherwise usable route into a failed request.
        logger.debug("Skipping request class: unable to verify named provider opt-in", exc_info=True)
        return False


def _with_class(kwargs: dict, request_class: str) -> dict:
    headers = {k: v for k, v in (kwargs.get("extra_headers") or {}).items() if k.lower() != HEADER.lower()}
    return {**kwargs, "extra_headers": {**headers, HEADER: request_class}}


def apply_turn_request_class(agent: Any, kwargs: dict) -> dict:
    if agent.api_mode not in _HTTP_MODES:
        return kwargs
    provider = agent.provider or ""
    if provider == "custom":
        provider = getattr(agent, "requested_provider", "") or ""
    if not _enabled_for_route(provider, str(agent.base_url or "")):
        return kwargs

    return _with_class(kwargs, turn_request_class(agent))


def apply_aux_request_class(client: Any, kwargs: dict, provider: str, api_mode: str | None, task: str | None) -> dict:
    if api_mode and api_mode not in _HTTP_MODES:
        return kwargs
    if _enabled_for_route(provider, str(getattr(client, "base_url", "") or "")):
        request_class = _AUX_REQUEST_CLASS.get() or ("background" if task in _BACKGROUND_TASKS else "interactive")
        return _with_class(kwargs, request_class)
    return kwargs
