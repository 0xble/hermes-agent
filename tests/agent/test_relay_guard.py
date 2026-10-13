"""Delegated children must return parent-directed content through native completion."""

from __future__ import annotations

from unittest.mock import patch

from agent.delegation_context import delegated_child_context, delegated_child_subprocess_env
from agent.relay_guard import (
    CHILD_PARENT_RELAY_ERROR,
    cli_child_parent_relay_error,
    child_parent_relay_error,
)
from gateway import session_context
from tools import mcp_tool_handlers


def _bind(session_id: str, profile: str = "default"):
    return session_context.set_session_vars(session_id=session_id, profile=profile)


def test_child_parent_is_refused_but_other_and_top_level_targets_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent = "parent-session"
    target = f"hermes:default/{parent}"
    tokens = _bind(parent)
    try:
        with delegated_child_context("child-session"):
            assert child_parent_relay_error(target) == CHILD_PARENT_RELAY_ERROR
            assert child_parent_relay_error("hermes:default/other-session") is None
            assert delegated_child_subprocess_env({})["HERMES_DELEGATED_PARENT_SESSION_ID"] == parent
        assert child_parent_relay_error(target) is None
    finally:
        session_context.clear_session_vars(tokens)


def test_mcp_parent_relay_is_blocked_before_call_server_acquisition(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = _bind("parent-session")
    try:
        with delegated_child_context("child-session"), patch(
            "tools.mcp_tool_handlers._acquire_call_server", side_effect=AssertionError("transport reached")
        ):
            result = mcp_tool_handlers._make_tool_handler("relay", "send", 30.0)({
                "address": "hermes:default/parent-session", "body": "do not send",
            })
        with delegated_child_context("child-session"), patch(
            "tools.mcp_tool_handlers._acquire_call_server", return_value=(None, "other target reached")
        ):
            other_result = mcp_tool_handlers._make_tool_handler("relay", "send", 30.0)({
                "address": "hermes:default/other-session", "body": "allowed",
            })
    finally:
        session_context.clear_session_vars(tokens)

    assert CHILD_PARENT_RELAY_ERROR in result
    assert other_result == "other target reached"


def test_relay_cli_parent_target_is_blocked_before_terminal_execution(tmp_path, monkeypatch):
    from tools import terminal_tool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = _bind("parent-session")
    try:
        with delegated_child_context("child-session"):
            with patch(
                "tools.terminal_tool.gateway_lifecycle_block", side_effect=AssertionError("execution reached")
            ):
                try:
                    terminal_tool._pre_exec_block(
                        "relay --json send hermes:default/parent-session",
                        env={}, env_type="local", cwd=str(tmp_path), workdir=None, session_key="",
                    )
                except terminal_tool._Rejected as rejected:
                    assert CHILD_PARENT_RELAY_ERROR in rejected.result_json
                else:
                    raise AssertionError("relay CLI was not blocked")
            assert cli_child_parent_relay_error("relay send hermes:default/other-session") is None
    finally:
        session_context.clear_session_vars(tokens)


def test_relay_cli_parent_target_is_blocked_with_attached_shell_operators(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = _bind("parent-session")
    try:
        with delegated_child_context("child-session"):
            for command in (
                "relay send hermes:default/parent-session; printf done",
                "relay send hermes:default/parent-session|printf done",
                "relay send hermes:default/parent-session>/dev/null",
            ):
                assert cli_child_parent_relay_error(command) == CHILD_PARENT_RELAY_ERROR
    finally:
        session_context.clear_session_vars(tokens)


def test_relay_cli_shell_operators_inside_target_arguments_are_not_reinterpreted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = _bind("parent-session")
    try:
        with delegated_child_context("child-session"):
            assert cli_child_parent_relay_error(
                "relay send 'hermes:default/parent-session;printf done'"
            ) is None
            assert cli_child_parent_relay_error(
                r"relay send hermes:default/parent-session\;printf done"
            ) is None
            assert cli_child_parent_relay_error(
                'relay send "hermes:default/parent-session"'
            ) == CHILD_PARENT_RELAY_ERROR
            assert cli_child_parent_relay_error(
                "relay send hermes:default/other-session>/dev/null"
            ) is None
        assert cli_child_parent_relay_error(
            "relay send hermes:default/parent-session>/dev/null"
        ) is None
    finally:
        session_context.clear_session_vars(tokens)


def test_top_level_mcp_parent_target_remains_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = _bind("parent-session")
    try:
        with patch("tools.mcp_tool_handlers._acquire_call_server", return_value=(None, "stub")):
            result = mcp_tool_handlers._make_tool_handler("relay", "send", 30.0)({
                "address": "hermes:default/parent-session", "body": "top level is legitimate",
            })
    finally:
        session_context.clear_session_vars(tokens)

    assert "child_parent_relay" not in result
    assert result == "stub"
