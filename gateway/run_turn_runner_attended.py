"""Which turns the attended-model request-boundary switch must leave alone."""
from __future__ import annotations


def attended_switch_exempt(runner, ctx, agent) -> bool:
    """Return True when the attended route must not rewrite this agent's request.

    Each owner below resolves its own route, and attended chat state must never replace it:
    a launch-policy session resolves its own frozen or followed route and effort, subagents and
    auxiliary agents inherit their parent's explicit choice, and a session pending bulk reset is
    about to be replaced.
    """
    from gateway.session_policy import policy_for_source
    if policy_for_source(runner, ctx.source) is not None:
        return True
    if getattr(agent, "is_subagent", False) or getattr(agent, "_is_auxiliary", False):
        return True
    return ctx.session_key in (getattr(runner, "_bulk_reset_pending_sessions", set()) or set())
