"""Shared goal admission: successful empty turns are still evaluation boundaries."""

from typing import Any, Optional


def is_successful_goal_turn(result: Any, response: str, *, succeeded: Optional[bool] = None) -> bool:
    """Prefer an explicit outcome; retain text fallback only for legacy callers.

    Display text is not a success signal: failure notices can be nonempty, while
    tool-only successful turns can be empty. A non-failed iteration handoff is
    resumable when its summary is available to the evaluator.
    """
    from agent.turn_failure_copy import is_max_iteration_handoff

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
