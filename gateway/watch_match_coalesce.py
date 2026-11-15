"""Coalesce held process watch matches before the gateway re-admits them."""

from __future__ import annotations


def coalesce_pending_watch_matches(events: list[dict]) -> list[dict]:
    """Keep one held match per process incarnation and destination, preserving its retry clock."""
    result, pending = [], {}
    for evt in events:
        if evt.get("type") != "watch_match" or not evt.get("session_id"):
            result.append(evt)
            continue
        key = tuple(evt.get(field) for field in (
            "session_id", "started_at", "session_key", "parent_session_id", "origin_hermes_home"))
        held = pending.get(key)
        if held is None:
            pending[key] = evt
            result.append(evt)
            continue
        # The producer bounds output. Absorb newer matches without resetting the old refusal
        # deadline or identity, otherwise fresh process output bypasses admission backoff.
        held["suppressed"] = int(held.get("suppressed") or 0) + int(evt.get("suppressed") or 0) + 1
        held["output"] = evt.get("output", "")
        held["pattern"] = evt.get("pattern", held.get("pattern", ""))
    return result
