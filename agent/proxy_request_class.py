"""Request attribution for explicitly opted-in CLIProxyAPI routes."""

from __future__ import annotations

import logging
from typing import Any

import httpx

HEADER = "X-Hermes-Request-Class"
_HTTP_MODES = {"chat_completions", "codex_responses", "anthropic_messages"}
logger = logging.getLogger(__name__)


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
    from hermes_cli.runtime_provider_custom import _get_named_custom_provider, _route_key
    from utils import base_url_hostname

    if provider in {"", "auto", "custom"} or not base_url.startswith(("https://", "http://")):
        return False
    if not base_url_hostname(base_url):
        return False
    try:
        entry = _get_named_custom_provider(provider, resolve_credentials=False)
        return bool(entry and entry.get("hermes_request_class") is True
                    and _route_key(entry.get("base_url")) == _route_key(base_url))
    except Exception:
        # A scheduling hint must not turn an otherwise usable route into a failed request.
        logger.debug("Skipping request class: unable to verify named provider opt-in")
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

    # Unlike billing initiator headers, this class describes the whole turn, including retries
    # and tool follow-ups. A fork/subagent never inherits its parent's interactive status.
    interactive = (getattr(agent, "_turn_provenance", "unknown") == "user"
                   and not getattr(agent, "_turn_origin", None)
                   and not getattr(agent, "is_subagent", False)
                   and not getattr(agent, "_delegate_depth", 0))
    return _with_class(kwargs, "interactive" if interactive else "background")


def apply_aux_request_class(client: Any, kwargs: dict, provider: str, api_mode: str | None) -> dict:
    if api_mode and api_mode not in _HTTP_MODES:
        return kwargs
    if _enabled_for_route(provider, str(getattr(client, "base_url", "") or "")):
        return _with_class(kwargs, "background")
    return kwargs
