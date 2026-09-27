"""Per-endpoint opt-in that lets a configured custom provider receive fast-mode params.

Fast params normally reach only the first-party endpoint that bills for them
(``models._fast_mode_route_supported``). A proxy that forwards them to that vendor, such as a
local subscription proxy, is opted in explicitly on its ``providers:`` entry::

    providers:
      codex-proxy:
        api: http://127.0.0.1:8317/v1
        api_mode: codex_responses
        capabilities:
          fast_mode: true

The opt-in is about cost, not compatibility, so it fails closed. It covers only models whose fast
params travel on the entry's own transport: Anthropic ``speed`` on ``anthropic_messages``, and
OpenAI or xAI ``service_tier`` on every other transport. Several entries often share one proxy URL
(one per transport). A route known only by its URL (bare ``custom``) is therefore opted in only
when every same-transport entry at that URL opts in.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

CAPABILITY = "fast_mode"
_ANTHROPIC_TRANSPORT = "anthropic_messages"


def _entries() -> List[Dict[str, Any]]:
    from hermes_cli.config import get_compatible_custom_providers, load_config_readonly

    try:
        return [entry for entry in get_compatible_custom_providers(load_config_readonly()) if isinstance(entry, dict)]
    except Exception:
        return []


def _carries_model_family(entry: Dict[str, Any], anthropic_model: bool) -> bool:
    return (str(entry.get("api_mode") or "").strip() == _ANTHROPIC_TRANSPORT) == anthropic_model


def _opted_in(entry: Dict[str, Any]) -> bool:
    return (entry.get("capabilities") or {}).get(CAPABILITY) is True


def custom_route_fast_mode_opted_in(
    provider: Optional[str], base_url: Optional[str], *, anthropic_model: bool) -> bool:
    """True when the configured custom endpoint behind this route opted into fast params."""
    from hermes_cli.providers import custom_provider_aliases
    from hermes_cli.route_identity import normalize_route_base_url

    requested = str(provider or "").strip().lower()
    entries = _entries()
    if requested.startswith("custom:"):
        named = [entry for entry in entries
                 if requested in custom_provider_aliases(str(entry.get("name") or ""), str(entry.get("provider_key") or ""))]
        return bool(named) and all(_opted_in(e) and _carries_model_family(e, anthropic_model) for e in named)
    if requested not in ("", "custom"):
        return False
    target = normalize_route_base_url(base_url)
    if not target:
        return False
    same_route = [entry for entry in entries
                  if normalize_route_base_url(entry.get("base_url")) == target
                  and _carries_model_family(entry, anthropic_model)]
    return bool(same_route) and all(_opted_in(entry) for entry in same_route)
