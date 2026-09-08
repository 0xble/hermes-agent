"""Least-privilege capability contract for native review children."""

from __future__ import annotations

from typing import Any


LEGACY_UNRESTRICTED = "legacy_unrestricted"
INSPECTION_ONLY = "inspection_only"
VALID_REVIEW_TOOL_POLICIES = frozenset({LEGACY_UNRESTRICTED, INSPECTION_ONLY})
INSPECTION_TOOL_NAMES = frozenset({"read_file", "search_files", "skills_list", "skill_view"})
PARENT_ONLY_REVIEW_TOOLS = frozenset({"review_current_work"})


def tool_name_from_definition(definition: Any) -> str:
    if not isinstance(definition, dict):
        return ""
    function = definition.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return str(definition.get("name") or "")


def remove_parent_only_review_tools(agent: Any) -> None:
    """Hide parent lifecycle controls from delegated children."""
    agent.tools = [
        definition for definition in (getattr(agent, "tools", None) or [])
        if tool_name_from_definition(definition) not in PARENT_ONLY_REVIEW_TOOLS
    ]
    valid = getattr(agent, "valid_tool_names", None)
    if isinstance(valid, set):
        valid.difference_update(PARENT_ONLY_REVIEW_TOOLS)


def apply_review_tool_policy(agent: Any, policy: str) -> None:
    """Freeze the child schema to inspection tools and disable MCP refresh.

    Final execution enforcement lives in ``agent.tool_executor``; filtering the
    schema is defense in depth and avoids advertising unavailable capabilities.
    """
    if policy not in VALID_REVIEW_TOOL_POLICIES:
        raise ValueError(f"Invalid review tool policy: {policy!r}")
    agent._review_tool_policy = policy
    if policy == LEGACY_UNRESTRICTED:
        return
    agent._skip_mcp_refresh = True
    agent.tools = [
        definition for definition in (getattr(agent, "tools", None) or [])
        if tool_name_from_definition(definition) in INSPECTION_TOOL_NAMES
    ]
    agent.valid_tool_names = {
        tool_name_from_definition(definition) for definition in agent.tools
    }


def review_registry_refresh_allowed(agent: Any) -> bool:
    """Fail closed at every final live-tool refresh surface."""
    return getattr(agent, "_review_tool_policy", None) != INSPECTION_ONLY


def review_tool_policy_block(agent: Any, tool_name: str) -> str | None:
    """Return a dispatch-block reason, or ``None`` when the call is allowed."""
    policy = getattr(agent, "_review_tool_policy", None)
    # Test doubles and third-party facades may synthesize arbitrary non-string
    # attributes. This module only installs explicit string policies.
    if not isinstance(policy, str):
        return None
    if policy in (None, LEGACY_UNRESTRICTED):
        return None
    if policy != INSPECTION_ONLY:
        return f"Invalid review tool policy {policy!r}; tool dispatch is denied."
    if tool_name not in INSPECTION_TOOL_NAMES:
        return (
            f"Tool {tool_name!r} is unavailable under the inspection-only review policy. "
            "The initial reviewer may read and search evidence but may not execute, mutate, "
            "delegate, dispatch MCP tools, or refresh tool registries."
        )
    return None
