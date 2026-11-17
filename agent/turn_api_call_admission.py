"""Pre-send token-per-minute admission for the conversation turn's provider call.

The last response's ``x-ratelimit-*`` headers say how many tokens the minute bucket still
holds. When the next request is predicted not to fit, sending it only earns a 429 that the
headers already announced, so the turn moves to its next available fallback first.

The prediction is advisory and fails open at every gap: no captured headers, headers captured
on a different route, no token estimate, or a bucket whose reset has already passed all admit
the request. One provider label can front several upstreams (a local proxy serving both
``gpt-*`` and ``muse-*``), so the bucket is matched on the whole route, never the provider
alone; otherwise the primary's bucket would also refuse the spare that shares its label.

The route is paired with the header state on the agent, not stored on the state itself: the
state is serialized into the usage payload, and an endpoint URL does not belong there.
"""
from __future__ import annotations

from typing import Any, Optional

from agent.rate_limit_tracker import tpm_admission_delay


def admission_route(agent: Any) -> tuple[str, str, str]:
    """The provider, endpoint and model one minute bucket belongs to."""
    return (
        str(getattr(agent, "provider", "") or "").strip().lower(),
        str(getattr(agent, "base_url", "") or "").strip().rstrip("/").lower(),
        str(getattr(agent, "model", "") or "").strip(),
    )


def remember_bucket_route(agent: Any, state: Any) -> None:
    """Pair a freshly captured header state with the route it was captured on."""
    agent._rate_limit_state_route = (state, admission_route(agent))


def predicted_tpm_overage(agent: Any, approx_tokens: Any) -> Optional[str]:
    """Describe a predicted minute-bucket overage on the live route, or None to admit."""
    state = getattr(agent, "_rate_limit_state", None)
    pair = getattr(agent, "_rate_limit_state_route", None)
    # The pairing must be for this exact state: one replaced since describes no known route.
    if state is None or not isinstance(pair, tuple) or len(pair) != 2 or pair[0] is not state:
        return None
    try:
        if pair[1] != admission_route(agent):
            return None
        if tpm_admission_delay(state, approx_tokens) <= 0:
            return None
        remaining, estimate = int(state.tokens_min.remaining), int(approx_tokens)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None  # a malformed snapshot is not evidence the request cannot fit
    return (f"{getattr(agent, 'model', '') or 'The model'} has only {remaining:,} tokens left in its "
            f"minute window; this request needs about {estimate:,}.")
