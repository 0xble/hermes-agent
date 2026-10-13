"""Shared guard for relay sends made from delegated child execution."""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any

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


def child_parent_relay_error(target: Any) -> str | None:
    """Return actionable denial text when a delegated child targets its own parent."""
    from agent.delegation_context import is_delegated_child_process_context

    if not is_delegated_child_process_context() or not isinstance(target, str):
        return None
    parent_session_id = _parent_session_id()
    profile = _profile_name().strip()
    if not parent_session_id or not profile:
        return None
    expected = f"hermes:{profile}/{parent_session_id}"
    return CHILD_PARENT_RELAY_ERROR if target.strip() == expected else None


def mcp_child_parent_relay_error(server_name: str, tool_name: str, args: Any) -> str | None:
    """Apply the guard to the Relay MCP server before its call server is acquired."""
    if server_name.strip().lower() not in {"relay", "mcp__relay"} or tool_name != "send":
        return None
    address = args.get("address") if isinstance(args, dict) else None
    return child_parent_relay_error(address)


def cli_child_parent_relay_error(command: str) -> str | None:
    """Apply the guard to a direct ``relay send`` command before terminal execution.

    Tokenize shell punctuation separately so an operator attached to the target
    (for example ``parent-session>/dev/null``) cannot hide the target from the
    guard. Quoting and escaping still follow normal shell rules.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    relay_index = next(
        (i for i, token in enumerate(tokens) if Path(token).name == "relay"),
        None,
    )
    if relay_index is None:
        return None
    try:
        send_index = next(i for i in range(relay_index + 1, len(tokens)) if tokens[i] == "send")
    except StopIteration:
        return None
    for token in tokens[send_index + 1:]:
        if token.startswith("hermes:"):
            if error := child_parent_relay_error(token):
                return error
    return None
