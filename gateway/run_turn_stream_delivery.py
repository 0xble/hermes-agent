"""Whether a turn's final reply demonstrably reached the user through its stream consumer."""

from __future__ import annotations

from contextlib import suppress


def stream_confirmed_final_delivery(consumer, final_text: str, *, previewed: bool = False) -> bool:
    """True only when the actual final reply reached the user: a finalize call may carry only the
    last preview snapshot, so reconcile against the recorded payload — a demonstrable mismatch
    (False) overrides the flag; None keeps legacy trust."""
    if consumer is None:
        return False
    if getattr(consumer, "final_response_sent", False):
        matcher = getattr(consumer, "delivered_final_matches", None)
        if callable(matcher):
            with suppress(Exception):
                if matcher(final_text) is False:
                    return False
        return True
    # Exact-text match against what the consumer DURABLY delivered (commentary, segments, and the
    # visible prefix only once a real send landed) — safe without the ``previewed`` flag. The codex
    # app-server bridge delivers the final agentMessage through the commentary path and never sets
    # response_previewed (#74248 / #80519); gating on the flag re-sent every such reply. Mismatching
    # commentary still returns False, so a distinct final answer is never suppressed (#65919). Draft
    # frames are ephemeral and must not count: after draft streaming + a failed finalize send this
    # predicate must stay False so the fallback final send still fires (#51828 / #33793).
    has_delivered_text = getattr(consumer, "has_durably_delivered_text", None)
    if callable(has_delivered_text):
        with suppress(Exception):
            return bool(has_delivered_text(final_text))
    return False
