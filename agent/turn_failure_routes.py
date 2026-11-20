"""Which routes a failed turn ran out of, and which config list owns its backups.

The failure card used to name the whole configured chain and a depth role (``leaf``), neither of
which the reader can act on. These helpers report only the routes this fallback episode left or
found cooled, and the exact ``fallback_providers`` list that feeds the failing agent: the
model-map option a delegated child was routed by, the delegation default, cron, or the top level.
"""
from __future__ import annotations

import logging
from typing import Any

from hermes_constants import display_hermes_home

logger = logging.getLogger(__name__)


def _label(provider: Any, model: Any) -> str:
    provider, model = str(provider or "").strip(), str(model or "").strip()
    return f"{model} via {provider}" if provider and model else model or provider


def note_route_left(agent: Any, provider: Any, model: Any) -> None:
    """Record a route the turn just abandoned for a fallback. A new episode starts a new list."""
    left = getattr(agent, "_fallback_routes_left", None)
    if not isinstance(left, list) or getattr(agent, "_provider_fallback_active", False) is not True:
        left = []
    left.append((provider, model))
    agent._fallback_routes_left = left


def reset_routes_left(agent: Any) -> None:
    """Forget the abandoned routes once the primary is back: that episode is over."""
    agent._fallback_routes_left = []


def _cooled_visited_entries(agent: Any) -> list[str]:
    """Chain entries the walk already passed that another process holds in cooldown."""
    chain = getattr(agent, "_fallback_chain", None)
    index = getattr(agent, "_fallback_index", 0)
    if not isinstance(chain, (list, tuple)) or not isinstance(index, int) or index <= 0:
        return []
    from agent.shared_primary_probe_runtime import fallback_route_is_active
    cooled = []
    for entry in chain[:index]:
        if not isinstance(entry, dict) or not entry.get("model"):
            continue
        try:
            active = fallback_route_is_active(entry.get("provider"), entry.get("base_url"), entry.get("model"))
        except Exception:
            # Failure copy is built on a path that is already reporting an error; a lookup
            # that fails here must never replace that error with its own.
            logger.debug("Cooldown lookup for a failure card failed", exc_info=True)
            continue
        if active:
            cooled.append(_label(entry.get("provider"), entry.get("model")))
    return cooled


def exhausted_route_labels(agent: Any, provider: Any, model: Any) -> list[str]:
    """Routes this episode ran out of, in order: those it left, the one that just failed, and
    visited entries it had to skip as cooled. Entries the walk never reached are not listed."""
    left = getattr(agent, "_fallback_routes_left", None)
    in_episode = getattr(agent, "_provider_fallback_active", False) is True
    labels = [_label(p, m) for p, m in left] if in_episode and isinstance(left, list) else []
    labels.append(_label(provider, model))
    labels.extend(_cooled_visited_entries(agent))
    return [label for label in dict.fromkeys(labels) if label]


def fallback_chain_path(agent: Any) -> str:
    """Where the failing agent's backup list is configured, as chat copy."""
    config = f"`{display_hermes_home()}/config.yaml`"
    route = getattr(agent, "_delegate_route", None)
    if isinstance(route, dict) and route.get("backend") == "model_alias" and isinstance(route.get("label"), str):
        return (f"`fallback_providers` of the `{route['label']}` model alias (under `model_aliases` or "
                f"`model.aliases`) in {config}")
    option = getattr(agent, "_delegate_model_map_option", None)
    if isinstance(option, str) and option.strip():
        return (f"`fallback_providers` of the `{option.strip()}` option in "
                f"`delegation.backends.model_map.options` ({config})")
    depth = getattr(agent, "_delegate_depth", 0)
    if isinstance(depth, int) and depth > 0:
        return f"`delegation.fallback_providers` in {config}"
    if getattr(agent, "platform", None) == "cron":
        return f"`cron.fallback_providers` in {config}"
    return f"`fallback_providers` in {config}"
