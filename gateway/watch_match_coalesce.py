"""Coalesce held process watch matches before the gateway re-admits them."""

from __future__ import annotations


def _retry_step(evt: dict) -> int:
    return int(evt.get("_watch_retry_step") or 0)


def coalesce_pending_watch_matches(events: list[dict]) -> list[dict]:
    """Keep one held match per process incarnation and destination, preserving its retry clock."""
    result, groups = [], {}
    for evt in events:
        if evt.get("type") != "watch_match" or not evt.get("session_id"):
            result.append(evt)
            continue
        key = tuple(evt.get(field) for field in (
            "session_id", "started_at", "session_key", "parent_session_id", "origin_hermes_home"))
        if key not in groups:
            groups[key] = []
            result.append(groups[key])
        groups[key].append(evt)
    return [_fold(item) if isinstance(item, list) else item for item in result]


def _fold(group: list[dict]) -> dict:
    # Queue order is not match age: a fresh match can be queued while an older refused one is
    # still awaiting injection, and only then is the older one requeued. A refusal advances the
    # retry step, so the furthest-backed-off event is the held one (earliest arrival on a tie) and
    # the latest arrival among the least-retried is the newest output. The held event keeps its
    # identity and retry clock, otherwise fresh process output bypasses admission backoff.
    held = max(group, key=_retry_step)
    newest = min(reversed(group), key=_retry_step)
    # The producer bounds output. Absorbed matches only add to the suppressed count.
    for evt in group:
        if evt is not held:
            held["suppressed"] = int(held.get("suppressed") or 0) + int(evt.get("suppressed") or 0) + 1
    if newest is not held:
        held["output"] = newest.get("output", "")
        held["pattern"] = newest.get("pattern", held.get("pattern", ""))
    return held
