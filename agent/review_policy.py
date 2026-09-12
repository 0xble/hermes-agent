"""Least-privilege capability contract for native review children."""

from __future__ import annotations

from typing import Any


LEGACY_UNRESTRICTED = "legacy_unrestricted"
INSPECTION_ONLY = "inspection_only"
VALID_REVIEW_TOOL_POLICIES = frozenset({LEGACY_UNRESTRICTED, INSPECTION_ONLY})
INSPECTION_TOOL_NAMES = frozenset({"read_file", "search_files", "skills_list", "skill_view"})
PARENT_ONLY_REVIEW_TOOLS = frozenset({"review_changes"})


def tool_name_from_definition(definition: Any) -> str:
    if not isinstance(definition, dict):
        return ""
    function = definition.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return str(definition.get("name") or "")


def remove_parent_only_review_tools(agent: Any) -> None:
    """Install the persistent parent-only contract on a delegated child."""
    agent._tool_contract_excluded_names = PARENT_ONLY_REVIEW_TOOLS
    tools, names = filter_child_tool_snapshot(agent, getattr(agent, "tools", None) or [],
                                               getattr(agent, "valid_tool_names", None))
    agent.tools = tools
    agent.valid_tool_names = names


def filter_child_tool_snapshot(agent: Any, definitions: Any, names: Any = None) -> tuple[list, set]:
    """Apply a child's durable tool exclusions to a staged snapshot.

    MCP refreshes and eviction restores construct a new list, so removing a
    parent-only tool only at child creation is not sufficient.  The contract is
    agent-scoped (never a mutable registry/global policy), and ordinary children
    remain eligible for normal refreshes.
    """
    excluded = getattr(agent, "_tool_contract_excluded_names", frozenset())
    if not isinstance(excluded, (set, frozenset)):
        excluded = frozenset()
    tools = [
        definition for definition in (definitions or [])
        if tool_name_from_definition(definition) not in excluded
    ]
    if not isinstance(names, (set, frozenset)):
        names = {tool_name_from_definition(definition) for definition in tools if tool_name_from_definition(definition)}
    return tools, set(names) - excluded


def apply_review_tool_policy(agent: Any, policy: str) -> None:
    """Freeze the child schema to inspection tools and disable MCP refresh.

    Final execution enforcement lives in ``agent.tool_executor``; filtering the
    schema is defense in depth and avoids advertising unavailable capabilities.
    """
    if policy not in VALID_REVIEW_TOOL_POLICIES:
        raise ValueError(f"Invalid review tool policy: {policy!r}")
    remove_parent_only_review_tools(agent)
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
    excluded = getattr(agent, "_tool_contract_excluded_names", frozenset())
    if isinstance(excluded, (set, frozenset)) and tool_name in excluded:
        return f"Tool {tool_name!r} is parent-only and cannot be invoked by a delegated child."
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
