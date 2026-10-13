"""Shared goal admission: successful empty turns are still evaluation boundaries."""

from typing import Any, Optional


def model_final_response(result: Any, fallback: str = "") -> str:
    """Explicit model text wins over delivery copy, including empty output."""
    if isinstance(result, dict) and "final_response" in result:
        # Gateway media repair can decorate final_response before delivery. Its
        # immutable pre-decoration copy carries the same model boundary.
        text = result.get("_model_final_response", result["final_response"])
        return text if isinstance(text, str) else ""
    return fallback if isinstance(fallback, str) else ""


def is_successful_goal_turn(result: Any, response: str, *, succeeded: Optional[bool] = None) -> bool:
    """Prefer an explicit outcome; retain text fallback only for legacy callers.

    Display text is not a success signal: failure notices can be nonempty, while
    tool-only successful turns can be empty. A non-failed iteration handoff is
    resumable when its summary is available to the evaluator.
    """
    from agent.turn_failure_copy import is_max_iteration_handoff

    response = model_final_response(result, response)
    if succeeded is False:
        return False
    if isinstance(result, dict):
        if result.get("failed") or result.get("interrupted") or result.get("error") or result.get("partial"):
            return False
        handoff = is_max_iteration_handoff(result) and bool(response.strip())
        if result.get("completed") is False and not handoff:
            return False
        if result.get("completed") is True or handoff:
            return True
    if succeeded is True:
        return True
    return bool(response.strip())
