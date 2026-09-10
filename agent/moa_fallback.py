"""Bounded, explicitly configured physical-route recovery for MoA slots."""
from typing import Any


def slot_candidates(slot: dict[str, Any]) -> list[dict[str, Any]]:
    return [slot, *(slot.get("fallback_models") or [])]


def physical_slots(preset: dict[str, Any]) -> list[dict[str, Any]]:
    return [candidate for slot in [*(preset.get("reference_models") or []), preset.get("aggregator") or {}]
            for candidate in slot_candidates(slot)]


def fallback_reason(exc: Exception) -> str | None:
    """Never recover authorization/configuration/programming failures by changing model."""
    from agent.auxiliary_client import _is_connection_error, _is_model_incompatible_error
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return None
    if status in (402, 429):
        return "quota/rate limit"
    if status in (408, 500, 502, 503, 504, 529):
        return "provider unavailable"
    if status == 404 and any(marker in str(exc).lower() for marker in ("model_not_found", "model not found", "model unavailable")):
        return "model unavailable"
    if _is_model_incompatible_error(exc):
        return "model unavailable"
    if status is not None:
        return None
    if isinstance(exc, (TimeoutError, ConnectionError)) or _is_connection_error(exc):
        return "transport unavailable"
    return None


def prefetched_stream(first: Any, iterator: Any, source: Any):
    try:
        yield first
        yield from iterator
    finally:
        close = getattr(source, "close", None)
        if close is not None:
            close()


def run_slot_chain(slot: dict[str, Any], call) -> Any:
    """Try each approved candidate once; same-route transport retries belong to call_llm."""
    candidates = slot_candidates(slot)
    for index, candidate in enumerate(candidates):
        try:
            return call(candidate)
        except Exception as exc:
            reason = fallback_reason(exc)
            if reason is None:
                raise
            if index + 1 == len(candidates):
                # Provider exception bodies can contain request credentials. Keep the
                # user-facing chain failure nonsecret and the original cause internal.
                raise RuntimeError(f"MoA slot chain exhausted ({len(candidates)} candidates; {reason})") from exc
