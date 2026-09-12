"""Opt-in TTS provider fallback; content/configuration errors never change routes."""


def provider_chain(config, primary, *, explicit_override=False):
    from tools.tts_command_provider import BUILTIN_TTS_PROVIDERS, _resolve_command_provider_config
    from tools.tts_tool_plugins import _lookup_plugin_provider

    raw = [] if explicit_override else config.get("fallback_providers", [])
    if not isinstance(raw, list) or any(not isinstance(p, str) or not p.strip() for p in raw):
        raise ValueError("tts.fallback_providers must be a list of provider names")
    chain = [primary]
    for name in raw:
        name = name.strip().lower()
        if name not in chain:
            chain.append(name)
    # Preserve legacy single-provider behavior, but never interpret an unknown
    # fallback as Edge (the historical built-in dispatcher default).
    if raw:
        for name in chain:
            if (name not in BUILTIN_TTS_PROVIDERS
                    and _resolve_command_provider_config(name, config) is None
                    and _lookup_plugin_provider(name) is None):
                raise ValueError(f"Unknown or unavailable TTS provider: {name}")
    return chain


def is_availability_failure(exc):
    import httpx
    import requests
    from openai import APIConnectionError
    from tools.tts_command_provider import TTSCommandDependencyUnavailable, TTSCommandTimeout

    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status in (408, 429) or 500 <= status < 600
    return isinstance(exc, (TTSCommandDependencyUnavailable, TTSCommandTimeout, TimeoutError, ConnectionError, httpx.TimeoutException, httpx.NetworkError,
                            requests.exceptions.ConnectionError, requests.exceptions.Timeout, APIConnectionError))
