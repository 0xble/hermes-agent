"""Shared guard for relay sends made from delegated child execution.

The Relay MCP call refuses the immediate parent and lineage topic session using
in-process delegation context. The ``relay`` CLI checks its parsed target against
:data:`RELAY_PARENT_ADDRESS_ENV`, carrying the topic address when resolved or the
immediate parent otherwise. This cooperative CLI
guard does not parse shell text. A nested command that clears or overrides the
variable loses the CLI guard, so it is not an arbitrary-shell security boundary.
"""

from __future__ import annotations

from typing import Any

# Read by relay's CLI ``send`` (relay C27); its refusal text matches CHILD_PARENT_RELAY_ERROR.
RELAY_PARENT_ADDRESS_ENV = "RELAY_PARENT_ADDRESS"

CHILD_PARENT_RELAY_ERROR = (
    "child_parent_relay: put this in your final result; the parent receives it when you finish"
)


def _profile_name() -> str:
    try:
        from gateway.session_context import bound_session_env
        if profile := bound_session_env("HERMES_SESSION_PROFILE"):
            return profile
    except Exception:
        pass
    try:
        from hermes_cli.profiles import get_active_profile_name
        return str(get_active_profile_name() or "")
    except Exception:
        return ""


def _parent_session_id() -> str:
    from agent.delegation_context import delegated_child_parent_session_id
    return delegated_child_parent_session_id().strip()


def child_parent_relay_address() -> str:
    """The immediate parent's relay address while a delegated child runs, else ``""``."""
    from agent.delegation_context import is_delegated_child_process_context

    if not is_delegated_child_process_context():
        return ""
    parent_session_id = _parent_session_id()
    profile = _profile_name().strip()
    if not parent_session_id or not profile:
        return ""
    return f"hermes:{profile}/{parent_session_id}"


def child_topic_relay_address() -> str:
    """The address of the session that started this in-process delegation chain, else ``""``.

    Carried in delegation context rather than read from session rows, because child
    rows on TUI and desktop carry the surface's source and look like topic sessions.
    Spawned subprocesses inherit the stamped CLI address instead.
    """
    from agent.delegation_context import delegated_child_root_session_id

    root = delegated_child_root_session_id().strip()
    profile = _profile_name().strip()
    if not root or not profile:
        return ""
    return f"hermes:{profile}/{root}"


def child_parent_relay_error(target: Any) -> str | None:
    """Deny a delegated child's send to its immediate parent or lineage topic."""
    if not isinstance(target, str):
        return None
    target = target.strip()
    expected = child_parent_relay_address()
    if expected and target == expected:
        return CHILD_PARENT_RELAY_ERROR
    topic = child_topic_relay_address()
    return CHILD_PARENT_RELAY_ERROR if topic and target == topic else None


def mcp_child_parent_relay_error(server_name: str, tool_name: str, args: Any) -> str | None:
    """Apply the guard to the Relay MCP server before its call server is acquired."""
    if server_name.strip().lower() not in {"relay", "mcp__relay"} or tool_name != "send":
        return None
    address = args.get("address") if isinstance(args, dict) else None
    return child_parent_relay_error(address)
