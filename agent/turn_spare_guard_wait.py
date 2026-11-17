"""Bounded waiting when spare fallback admission defers a turn."""

from __future__ import annotations

import time
from typing import Any


def wait_for_deferred_spare_guard(
    agent: Any, retry: Any, *, messages: Any, conversation_history: Any, api_call_count: int,
    abort_message: str = "Interrupt detected during spare fallback wait, aborting.",
) -> dict[str, Any] | None:
    """Wait for the already-armed primary cooldown without adding an API retry.

    Spare admission can reject every fallback candidate.  The normal bounded retry budget has
    then been spent, so returning the error immediately would ignore the primary cooldown that
    the same failure armed.  Reuse the interruptible wait rail and return an interruption result;
    otherwise the caller continues its existing terminal path.
    """
    # Test doubles and lightweight agent shims often expose missing attributes as callables.
    # Only the guard's concrete ``True`` marker authorizes this extra wait; a truthy arbitrary
    # attribute must never alter the terminal, fallback, or auto-recovery paths.
    if getattr(agent, "_spare_guard_deferred", False) is not True:
        return None
    agent._spare_guard_deferred = False
    deadline = getattr(agent, "_rate_limited_until", 0)
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return None
    remaining = max(0.0, float(deadline) - time.monotonic())
    if remaining <= 0:
        return None
    from agent.turn_recovery import interruptible_backoff_sleep
    return interruptible_backoff_sleep(
        agent, remaining, retry, messages=messages, conversation_history=conversation_history,
        api_call_count=api_call_count, abort_message=abort_message,
        interrupt_text="Operation interrupted: waiting for the primary cooldown before retrying.",
        activity_label="spare fallback cooldown wait",
    )
