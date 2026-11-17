"""Bounded mid-turn primary re-check for a fallback episode that armed no cooldown.

A rate limit, billing refusal or overload arms a cooldown, and that deadline is what lets a long
turn return to the primary (``fallback_route_recovery``). A generic 5xx, a timeout, an empty
response or a pre-send admission switch arms nothing, so such an episode had no deadline at all
and lasted until the next user turn. A delegated child is one turn of hundreds of calls, so it
stayed on its spare for good.

These helpers give that episode a local re-check deadline with exponential spacing: the primary
is retried a bounded number of times, never per request. The deadline is per agent and in
memory. It never writes the shared cooldown file and never touches ``_rate_limited_until``,
which remain the cooled path's signals; the restore itself still goes through
``restore_primary_runtime`` and so honours an outage another process recorded.
"""
from __future__ import annotations

import time
from typing import Any

from agent.error_classifier import FailoverReason

UNCOOLED_RECHECK_BASE_S = 60.0
UNCOOLED_RECHECK_CAP_S = 900.0
# Reasons a later attempt can plausibly clear. The cooldown reasons are listed too: they normally
# arm their own deadline, and this is the backstop for the paths where that arm is skipped. A
# deterministic refusal (content policy, missing model, entitlement, context overflow) would
# fail every re-check the same way, so it stays turn-scoped.
_RECHECK_REASONS = frozenset({
    FailoverReason.timeout, FailoverReason.server_error, FailoverReason.overloaded,
    FailoverReason.rate_limit, FailoverReason.upstream_rate_limit, FailoverReason.unknown,
})


def _positive(raw: Any) -> float:
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0 else 0.0


def _recheck_delay(agent: Any) -> float:
    """The agent's current spacing; anything that is not a positive number means none yet."""
    return _positive(getattr(agent, "_uncooled_recheck_delay", 0))


def begin_uncooled_episode(agent: Any, reason: Any) -> None:
    """Start a fallback episode as uncooled and schedule its first re-check when transient.

    A stale deadline from an older episode must not authorize recovery, so the cooled flag is
    cleared here; arming a cooldown afterwards sets it again and takes precedence.
    """
    agent._fallback_episode_cooled = False
    if reason is not None and reason not in _RECHECK_REASONS:
        agent._uncooled_recheck_at = None
        return
    now = time.monotonic()
    restored_at = getattr(agent, "_uncooled_restored_at", None)
    # The spacing in force when the primary was put back, not the live one: this runs before the
    # chain is walked, so an attempt that binds no fallback must leave the relapse readable, and
    # counted once, for the attempt that does (``note_fallback_activated`` then clears it).
    held = _positive(getattr(agent, "_uncooled_restored_delay", 0))
    # Failing again soon after a re-check put the primary back means the outage persists: double
    # the spacing. A primary that held for a full window has earned a fresh start.
    relapsed = (isinstance(restored_at, (int, float)) and held > 0
                and now - restored_at < max(held, UNCOOLED_RECHECK_BASE_S))
    delay = min(held * 2, UNCOOLED_RECHECK_CAP_S) if relapsed else UNCOOLED_RECHECK_BASE_S
    agent._uncooled_recheck_delay = delay
    agent._uncooled_recheck_at = now + delay


def note_fallback_activated(agent: Any) -> None:
    """A fallback now carries the turn, so the relapse that led to it has been counted."""
    agent._uncooled_restored_at = None


def uncooled_recheck_due(agent: Any) -> bool:
    """True once an uncooled episode's re-check deadline has passed."""
    deadline = getattr(agent, "_uncooled_recheck_at", None)
    return isinstance(deadline, (int, float)) and time.monotonic() >= deadline


def settle_uncooled_recheck(agent: Any, *, restored: bool) -> None:
    """Record a re-check's outcome so the next one is spaced, not immediate.

    A restore is remembered so a quick relapse widens the spacing. A refused or failed restore
    waits a full window before the next attempt instead of retrying on every request.
    """
    if getattr(agent, "_uncooled_recheck_at", None) is None:
        return
    now = time.monotonic()
    if restored:
        agent._uncooled_recheck_at = None
        agent._uncooled_restored_at = now
        agent._uncooled_restored_delay = _recheck_delay(agent)
        return
    agent._uncooled_recheck_at = now + (_recheck_delay(agent) or UNCOOLED_RECHECK_BASE_S)
