"""Context-window pinning and display resolution for model selection."""

from __future__ import annotations

import logging
from typing import Optional
from agent.models_dev import ModelInfo


logger = logging.getLogger('hermes_cli.model_switch')


def effective_context_pin(
    config_context_length: int | None, model: str, provider: str, base_url: str = "",
    configured_model: str | None = None, configured_provider: str | None = None,
    configured_base_url: str | None = None) -> int | None:
    """The ``model.context_length`` pin that still applies to ``model`` on this route, or None when the
    route changed from the configured one (the pin belongs to that model). Fail-closed: None on error."""
    if config_context_length is None or not (configured_model or configured_provider or configured_base_url):
        return config_context_length
    try:
        from hermes_cli.route_identity import should_clear_context_pin
        clear = should_clear_context_pin(
            configured_model, model, configured_base_url, base_url, configured_provider, provider)
    except Exception:  # any route-identity failure drops the pin, as resolve_display_context_length always did
        logger.debug("context pin route check failed for %s", model, exc_info=True)
        return None
    return None if clear else config_context_length


def resolve_display_context_length(
    model: str, provider: str, base_url: str = "", api_key: str = "",
    model_info: Optional[ModelInfo] = None, custom_providers: list | None = None,
    config_context_length: int | None = None, configured_model: str | None = None,
    configured_provider: str | None = None,
    configured_base_url: str | None = None, *, api_mode: str = "") -> Optional[int]:
    """Context length to show in /model output.

    models.dev reports per-vendor context but provider-enforced limits can be lower (Codex OAuth
    caps gpt-5.5 at 272k), so ``agent.model_metadata.get_model_context_length`` is authoritative
    (it also honors ``custom_providers[].models.<id>.context_length``); ``model_info.context_window``
    is the fallback. A ``config_context_length`` pin is dropped when the route changed.

    When ``custom_providers`` is provided, per-model ``context_length`` overrides from
    ``custom_providers[].models.<id>.context_length`` are honored — this closes #15779 where ``/model``
    switch ignored user-set overrides.
    """
    from hermes_cli.model_switch import effective_context_pin
    config_context_length = effective_context_pin(
        config_context_length, model, provider, base_url, configured_model, configured_provider, configured_base_url)
    try:
        from agent.model_metadata import get_model_context_length
        ctx = get_model_context_length(
            model, base_url=base_url or "", api_key=api_key or "", provider=provider or None,
            custom_providers=custom_providers, config_context_length=config_context_length, api_mode=api_mode)
        if ctx:
            return int(ctx)
    except Exception:
        pass
    if model_info is not None and model_info.context_window:
        return int(model_info.context_window)
    return None


async def resolve_display_context_length_async(model: str, provider: str, **kwargs) -> Optional[int]:
    """Thread-offloaded :func:`resolve_display_context_length` (same keyword arguments) — the sync
    version runs blocking provider probes that async gateway handlers must not run on the loop."""
    from hermes_cli.model_switch import resolve_display_context_length
    import asyncio
    return await asyncio.to_thread(resolve_display_context_length, model, provider, **kwargs)
