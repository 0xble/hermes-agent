"""Freeze inherited fallback authority once, during named-child preflight."""
from copy import deepcopy


def frozen_fallback_client(pin, entry):
    """Build a client from immutable authority without consulting any router/pool."""
    from agent.auxiliary_client import _create_openai_client
    from agent.codex_headers import codex_cloudflare_headers

    entry = {"request_overrides": {}, **entry}
    route = next((r for r in pin.fallback_routes if r.native_entry() == entry), None)
    if route is None:
        raise ValueError("fallback entry differs from frozen authority")
    import json
    from types import SimpleNamespace
    if route.api_mode == "anthropic_messages":
        # Native installation only needs these attributes; do not allocate an
        # unused OpenAI/httpx client for an Anthropic route.
        return SimpleNamespace(api_key=route.api_key, base_url=route.base_url)
    headers = json.loads(route.request_overrides_json).get("extra_headers", {})
    if route.api_mode == "codex_responses":
        headers = {**headers, **codex_cloudflare_headers(route.api_key, base_url=route.base_url)}
    return _create_openai_client(api_key=route.api_key, base_url=route.base_url,
                                 default_headers=headers, max_retries=0)



def freeze_parent_fallback_routes(parent, primary_provider, primary_model):
    from agent.reasoning_effort import clamp_effort, requested_effort, transport_supported_reasoning_efforts
    from hermes_cli.config import load_config
    from hermes_cli.fallback_config import resolve_entry_api_key
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_constants import resolve_reasoning_config
    from tools.custom_subagents import FallbackDefinition, _freeze_fallback_runtime
    from tools.delegate_tool_config import _merge_request_overrides

    remaining = deepcopy((getattr(parent, "_fallback_chain", None) or [])[getattr(parent, "_fallback_index", 0):])
    pin = getattr(parent, "_delegation_runtime_pin", None)
    if pin is not None:
        # A named parent already has immutable authority. Never re-resolve it
        # through today's provider config, even for a newly launched grandchild.
        routes = []
        for entry in remaining:
            route = next((r for r in pin.fallback_routes if r.native_entry() == entry), None)
            if route is None:
                raise ValueError("parent fallback chain differs from its frozen authority")
            routes.append(route)
        return tuple(routes)

    config = load_config() if remaining else {}
    frozen = []
    seen = {(primary_provider, primary_model)}
    for index, entry in enumerate(remaining):
        provider, model = entry.get("provider"), entry.get("model")
        if not provider or not model:
            raise ValueError(f"parent fallback {index} has no provider/model")
        if (provider, model) in seen:
            # RuntimePin identifies routes by provider/model; distinct endpoints
            # with that identity cannot be represented safely. Refuse, don't drop.
            raise ValueError(f"parent fallback {index} repeats a provider/model identity")
        try:
            runtime = resolve_runtime_provider(
                requested=provider, target_model=model,
                explicit_base_url=entry.get("base_url"), explicit_api_key=resolve_entry_api_key(entry),
            )
        except Exception as exc:
            raise ValueError(f"parent fallback {index} cannot be authorized") from exc
        if entry.get("api_mode"):
            runtime = {**runtime, "api_mode": entry["api_mode"]}
        runtime = {**runtime, "request_overrides": _merge_request_overrides(
            runtime.get("request_overrides"), entry.get("request_overrides"))}
        effort = entry.get("reasoning_effort")
        if effort is None:
            reasoning = resolve_reasoning_config(config, model)
            effort = "none" if reasoning and reasoning.get("enabled") is False else requested_effort(reasoning)
            supported = transport_supported_reasoning_efforts(
                runtime.get("provider") or provider, model, runtime.get("api_mode"))
            effort = clamp_effort(effort, supported) if supported else None
        frozen.append(_freeze_fallback_runtime(
            runtime, FallbackDefinition(provider, model, effort), f"parent fallback {index}"))
        seen.add((provider, model))
    return tuple(frozen)
