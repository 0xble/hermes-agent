"""Compatibility helper for explicit agent stop producers."""

from __future__ import annotations

import inspect
from typing import Any


SYSTEM_INTERRUPT_SOURCE = "system"
_USER_REASONS = frozenset({"explicit stop requested", "user sent a new message", "user interrupt"})


def _accepts_keyword(callable_obj: Any, name: str) -> bool:
    """Return whether a callable explicitly supports a keyword argument."""
    try:
        parameters = inspect.signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        or (p.name == name and p.kind is not inspect.Parameter.POSITIONAL_ONLY)
        for p in parameters
    )


def _user_interrupt_pending(agent: Any) -> bool:
    return bool(getattr(agent, "_interrupt_requested", False)
                and getattr(agent, "_tool_interrupt_reason", None) in _USER_REASONS)


def request_soft_interrupt(
    agent: Any, message: str | None = None, *, interrupt_source: str | None = None
) -> bool:
    """Call ``agent.interrupt``, passing ``interrupt_source`` only when it is accepted."""
    interrupt = getattr(agent, "interrupt", None)
    if not callable(interrupt):
        return False
    if interrupt_source is not None and _accepts_keyword(interrupt, "interrupt_source"):
        interrupt(message, interrupt_source=interrupt_source)
    else:
        interrupt(message)
    return True


def request_hard_interrupt(
    agent: Any,
    message: str | None = None,
    *,
    tool_reason: str | None = None,
    delegation_reason: str | None = None,
    interrupt_source: str | None = None,
) -> bool:
    """Request an explicit stop, falling back to the legacy interrupt ABI.

    New agents expose ``hard_interrupt(message=None)``; third-party agents and old test
    doubles may only expose ``interrupt(message=None)`` and must not receive keyword
    arguments they do not know. ``tool_reason`` is a trusted, fixed category that may be
    exposed in model-visible tool cancellation output, forwarded only when the callable
    explicitly supports it. ``delegation_reason`` is trusted producer metadata stored on
    the agent for durable delegation classification; ``interrupt_source`` marks a system
    producer independently of its human-readable message. When omitted, an explicit direct
    stop defaults to ``cancel``. Returns ``False`` only when neither callable is available.
    """
    # Static lookup first: a dynamic ``__getattr__`` proxy (unspecced MagicMock, RPC
    # facade) must not be treated as genuinely implementing the new ABI.
    try:
        inspect.getattr_static(agent, "hard_interrupt")
    except AttributeError:
        interrupt = None
    else:
        interrupt = getattr(agent, "hard_interrupt", None)
    if not callable(interrupt):
        interrupt = getattr(agent, "interrupt", None)
    if not callable(interrupt):
        return False
    if interrupt_source is None and tool_reason is not None and tool_reason not in _USER_REASONS:
        interrupt_source = SYSTEM_INTERRUPT_SOURCE
    # Every explicit producer leaves a trusted stop category for delegation results.
    # _signal_child_stop supplies system causes first; do not turn shutdown into a user cancel.
    accepts_source = _accepts_keyword(interrupt, "interrupt_source")
    try:
        reason = delegation_reason or getattr(agent, "_delegation_interrupt_reason", None)
        if not isinstance(reason, str) or not reason:
            reason = "cancel"
        agent._delegation_interrupt_reason = reason
        # Let InterruptControlMixin's publication guard decide whether a system stop may
        # replace a pending user interrupt. Legacy callables have no source parameter, so
        # preserve the metadata only when no user interrupt is already pending.
        if interrupt_source and not accepts_source and not _user_interrupt_pending(agent):
            agent._interrupt_source = interrupt_source
    except Exception:
        pass  # metadata must not break legacy/immutable interrupt facades
    kwargs = {}
    if (interrupt_source == SYSTEM_INTERRUPT_SOURCE and not accepts_source
            and _user_interrupt_pending(agent)):
        # A legacy callable cannot apply the publication guard, so re-assert the pending user's
        # own message: the stop still escalates, but the system cause never replaces the user's.
        message, tool_reason = getattr(agent, "_interrupt_message", None), None
    if tool_reason is not None and _accepts_keyword(interrupt, "tool_reason"):
        kwargs["tool_reason"] = tool_reason
    if interrupt_source is not None and accepts_source:
        kwargs["interrupt_source"] = interrupt_source
    if message is None:
        interrupt(**kwargs)
    else:
        interrupt(message, **kwargs)
    return True
