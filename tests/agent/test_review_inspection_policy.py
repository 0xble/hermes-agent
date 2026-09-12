"""Enforcement tests for the native review inspection-only policy."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import review_engine
from agent import tool_executor
from agent.review_policy import remove_parent_only_review_tools
from toolsets import resolve_toolset
from tools import delegate_tool_toolsets


def test_review_inspection_toolset_is_static_read_and_search_only():
    names = set(resolve_toolset("review-inspection"))
    assert names == {"read_file", "search_files", "skills_list", "skill_view"}
    assert not any(name.startswith("mcp_") or name.startswith("mcp__") for name in names)


def test_review_policy_omitted_is_deliberately_legacy(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"auxiliary": {"review": {}}})
    assert review_engine._load_review_tool_policy() == "legacy_unrestricted"


def test_review_policy_accepts_explicit_modes(monkeypatch):
    for value in ("legacy_unrestricted", "inspection_only"):
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda value=value: {"auxiliary": {"review": {"tool_policy": value}}},
        )
        assert review_engine._load_review_tool_policy() == value


def test_review_policy_invalid_explicit_config_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"auxiliary": {"review": {"tool_policy": "read-mostly"}}},
    )
    with pytest.raises(ValueError, match="tool_policy"):
        review_engine._load_review_tool_policy()


def test_inspection_policy_disables_mcp_inheritance(monkeypatch):
    parent = SimpleNamespace(
        enabled_toolsets=["file", "skills", "mcp-danger"],
        disabled_toolsets=[],
    )
    monkeypatch.setattr(delegate_tool_toolsets, "_get_inherit_mcp_toolsets", lambda: True)
    monkeypatch.setattr(delegate_tool_toolsets, "_is_mcp_toolset_name", lambda name: name == "mcp-danger")
    enabled, _disabled = delegate_tool_toolsets._resolve_child_toolsets(
        parent,
        ["review-inspection"],
        "leaf",
        inherit_mcp_toolsets=False,
    )
    assert enabled == ["review-inspection"]
    assert "mcp-danger" not in enabled


def _agent(policy="inspection_only"):
    agent = MagicMock()
    agent._review_tool_policy = policy
    decision = MagicMock()
    decision.allows_execution = True
    agent._tool_guardrails.before_call.return_value = decision
    return agent


def _ref(name):
    return tool_executor._ToolCallRef(name, {}, "task", "call", [])


def test_final_dispatch_blocks_execution_mutation_delegation_mcp_and_refresh(monkeypatch):
    monkeypatch.setattr(tool_executor, "_pre_tool_block", lambda *_args: (None, {}))
    forbidden = [
        "terminal", "execute_code", "write_file", "patch", "delegate_task",
        "mcp_server_write", "setup_mcp", "refresh_agent_mcp_tools",
    ]
    for name in forbidden:
        called = []
        state = tool_executor._ManagedToolResult(None, {}, [], False, False)
        result = tool_executor._dispatch_authorized_once(
            _agent(), state, _ref(name),
            execute=lambda _args: called.append(name),
            scope_block=None, display_index=None, begin_execution=None, authorization_gate=None,
        )
        assert called == [], name
        assert "inspection-only" in json.loads(result)["error"].lower()
        assert state.blocked is True


def test_final_dispatch_allows_exact_inspection_tools(monkeypatch):
    monkeypatch.setattr(tool_executor, "_pre_tool_block", lambda *_args: (None, {}))
    for name in ("read_file", "search_files", "skills_list", "skill_view"):
        state = tool_executor._ManagedToolResult(None, {}, [], False, False)
        result = tool_executor._dispatch_authorized_once(
            _agent(), state, _ref(name),
            execute=lambda _args, name=name: f"ran:{name}",
            scope_block=None, display_index=None, begin_execution=None, authorization_gate=None,
        )
        assert result == f"ran:{name}"
        assert state.blocked is False


def test_unknown_runtime_policy_fails_closed_at_dispatch(monkeypatch):
    monkeypatch.setattr(tool_executor, "_pre_tool_block", lambda *_args: (None, {}))
    called = []
    state = tool_executor._ManagedToolResult(None, {}, [], False, False)
    result = tool_executor._dispatch_authorized_once(
        _agent("misspelled"), state, _ref("read_file"),
        execute=lambda _args: called.append(True),
        scope_block=None, display_index=None, begin_execution=None, authorization_gate=None,
    )
    assert called == []
    assert "invalid review tool policy" in json.loads(result)["error"].lower()


def test_non_string_synthetic_policy_attribute_does_not_block_normal_agent(monkeypatch):
    monkeypatch.setattr(tool_executor, "_pre_tool_block", lambda *_args: (None, {}))
    agent = _agent()
    agent._review_tool_policy = MagicMock()
    state = tool_executor._ManagedToolResult(None, {}, [], False, False)
    assert tool_executor._dispatch_authorized_once(
        agent, state, _ref("read_file"), execute=lambda _args: "ran",
        scope_block=None, display_index=None, begin_execution=None, authorization_gate=None,
    ) == "ran"


def test_delegated_child_schema_hides_parent_only_review_tool():
    child = SimpleNamespace(
        tools=[{"function": {"name": "review_changes"}}, {"function": {"name": "read_file"}}],
        valid_tool_names={"review_changes", "read_file"},
    )
    remove_parent_only_review_tools(child)
    assert [tool["function"]["name"] for tool in child.tools] == ["read_file"]
    assert child.valid_tool_names == {"read_file"}


def test_parent_only_contract_blocks_a_child_even_if_a_stale_schema_reintroduces_it(monkeypatch):
    monkeypatch.setattr(tool_executor, "_pre_tool_block", lambda *_args: (None, {}))
    child = _agent("legacy_unrestricted")
    remove_parent_only_review_tools(child)
    called = []
    state = tool_executor._ManagedToolResult(None, {}, [], False, False)
    result = tool_executor._dispatch_authorized_once(
        child, state, _ref("review_changes"), execute=lambda _args: called.append(True),
        scope_block=None, display_index=None, begin_execution=None, authorization_gate=None,
    )
    assert called == [] and state.blocked is True
    assert "parent-only" in json.loads(result)["error"].lower()


def test_mcp_refresh_skipped_for_inspection_reviewer(monkeypatch):
    agent = SimpleNamespace(_skip_mcp_refresh=True)
    called = []
    monkeypatch.setattr(
        "tools.mcp_tool_agent.refresh_agent_mcp_tools",
        lambda *_args, **_kwargs: called.append(True),
    )
    from agent.conversation_compression import _refresh_agent_tool_definitions

    assert _refresh_agent_tool_definitions(agent) is False
    assert called == []


def test_final_mcp_refresh_surface_rejects_inspection_reviewer(monkeypatch):
    from tools.mcp_tool_agent import refresh_agent_mcp_tools

    agent = SimpleNamespace(_review_tool_policy="inspection_only")
    monkeypatch.setattr(
        "model_tools.get_tool_definitions",
        lambda **_kwargs: pytest.fail("registry rebuild must not start"),
    )
    assert refresh_agent_mcp_tools(agent) == set()
