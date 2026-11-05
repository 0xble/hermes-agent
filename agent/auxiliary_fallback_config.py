"""Settings carried by concrete auxiliary fallback candidates."""
import logging
import re
from typing import Any, Optional

logger = logging.getLogger("agent.auxiliary_client")

def _fallback_entry_timeout(task: Optional[str], fb_label: str) -> Optional[float]:
    """Per-entry ``timeout`` for a configured fallback candidate, or None (keep the task-level
    timeout). Inheriting the primary's deadline used to kill healthy-but-slower fallbacks.

    A fallback candidate previously inherited the exact timeout the primary provider was called with. When
    that deadline was tuned for the primary (or the primary simply consumed its whole budget before failing
    over), the fallback aborted on the same clock even when independently healthy — a 163k-token compression
    that needs ~90s on the fallback died at the primary's 30s deadline every turn (#62452).
    """
    from agent.auxiliary_client import _fallback_chain_entry, _coerce_positive_timeout
    entry = _fallback_chain_entry(task, fb_label)
    return _coerce_positive_timeout(entry.get("timeout") if entry else None)


def _fallback_reasoning_config(task: Optional[str], fb_label: str, primary: Optional[dict], client: Any = None) -> Optional[dict]:
    """Use the selected fallback route's effort, including main-chain candidates."""
    from agent.auxiliary_client import _fallback_chain_entry
    entry = _fallback_chain_entry(task, fb_label)
    attached_effort = getattr(client, "_hermes_fallback_reasoning_effort", None)
    if entry is None and isinstance(attached_effort, str):
        entry = {"reasoning_effort": attached_effort}
    if not isinstance(entry, dict) or "reasoning_effort" not in entry:
        return primary
    from hermes_constants import parse_reasoning_effort
    parsed = parse_reasoning_effort(entry["reasoning_effort"])
    if parsed is None:
        logger.warning("Auxiliary %s: %s has invalid fallback reasoning_effort %r; retaining primary setting",
                       task or "call", fb_label, entry["reasoning_effort"])
        return primary
    return parsed


def _fallback_provider_from_label(label: str) -> str:
    """Recover the provider identifier from a fallback display label."""
    match = re.match(r"(?:fallback_chain\[\d+\]|fallback_providers\[\d+\]|main-agent)\(([^)]+)\)$", label or "")
    return match.group(1).strip() if match else str(label or "").strip()
