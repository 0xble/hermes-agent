"""Delegated children must return parent-directed content through native completion."""

from __future__ import annotations

from unittest.mock import patch

import pytest

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


def test_relay_cli_heredoc_body_is_not_a_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = _bind("parent-session")
    try:
        with delegated_child_context("child-session"):
            assert cli_child_parent_relay_error(
                "relay --json send hermes:default/parent-session <<EOF\n"
                "📨 Result ready\n"
                "Here's the result.\n"
                "EOF"
            ) == CHILD_PARENT_RELAY_ERROR
            assert cli_child_parent_relay_error(
                "relay send hermes:default/other-session <<EOF\n"
                "hermes:default/parent-session\n"
                "EOF\n"
                "printf 'hermes:default/parent-session'"
            ) is None
    finally:
        session_context.clear_session_vars(tokens)


def test_relay_cli_ambiguous_send_syntax_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = _bind("parent-session")
    try:
        with delegated_child_context("child-session"):
            assert cli_child_parent_relay_error("relay send") == CHILD_PARENT_RELAY_ERROR
            assert cli_child_parent_relay_error(
                "relay send hermes:default/parent-session <<EOF\nunfinished"
            ) == CHILD_PARENT_RELAY_ERROR
    finally:
        session_context.clear_session_vars(tokens)


@pytest.mark.parametrize("command", [
    "relay send hermes:default/other-session hermes:default/parent-session",
    "relay send hermes:default/other-session 'hermes:default/parent-session'",
    "relay send hermes:default/other-session --task hermes:default/parent-session",
    "relay send --task hermes:default/parent-session hermes:default/other-session",
    "relay --fields hermes:default/parent-session send hermes:default/other-session",
    "relay send hermes:default/other-session; printf hermes:default/parent-session",
    "relay send hermes:default/other-session&&printf hermes:default/parent-session",
    "relay send hermes:default/other-session||printf hermes:default/parent-session",
    "relay send hermes:default/other-session|printf hermes:default/parent-session",
    "relay send hermes:default/other-session& printf hermes:default/parent-session",
    "relay send hermes:default/other-session\nprintf hermes:default/parent-session",
    "relay send hermes:default/other-session>hermes:default/parent-session",
    "relay send hermes:default/other-session 2>hermes:default/parent-session",
    "relay send hermes:default/other-session#hermes:default/parent-session",
    "relay send hermes:default/other-session;# relay send hermes:default/parent-session",
    "printf '%s' relay send hermes:default/parent-session",
    "printf ';' relay send hermes:default/parent-session",
    "printf 'relay send hermes:default/parent-session'",
    "printf relay; printf send hermes:default/parent-session",
    "relay status receipt; printf send hermes:default/parent-session",
    "relay status receipt send hermes:default/parent-session",
    "relay send hermes:default/other-session <<EOF\nHere's hermes:default/parent-session\nEOF",
    "relay send hermes:default/other-session <<'EOF'\nrelay send hermes:default/parent-session\nEOF",
    "relay send hermes:default/other-session <<-'EOF'\n\tHere's hermes:default/parent-session\n\tEOF",
    "relay send hermes:default/other-session <<A <<B\nHere's result\nA\nhermes:default/parent-session\nB",
    "cat <<'EOF'\nrelay send hermes:default/parent-session\nEOF",
    "relay send hermes:default/other-session <<EOF\n$(printf done) relay send hermes:default/parent-session\nEOF",
    "relay send hermes:default/other-session <<EOF\n$(printf hermes:default/parent-session)\nEOF",
    "relay send hermes:default/other-session <<EOF\n$(relay send hermes:default/other-session)\nEOF",
    "relay send hermes:default/other-session <<EOF\n`printf hermes:default/parent-session`\nEOF",
    "relay send hermes:default/other-session <<'EOF'\n$(relay send hermes:default/parent-session)\nEOF",
    "relay send hermes:default/other-session <<EOF\n\\$(relay send hermes:default/parent-session)\nEOF",
    "relay send 'hermes:default/parent-\\\nsession'",
])
def test_relay_cli_only_actual_destination_is_checked(command, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = _bind("parent-session")
    try:
        with delegated_child_context("child-session"):
            assert cli_child_parent_relay_error(command) is None
    finally:
        session_context.clear_session_vars(tokens)


@pytest.mark.parametrize("command", [
    "printf done;relay send hermes:default/parent-session",
    "relay send hermes:default/other-session;relay send hermes:default/parent-session",
    "relay send hermes:default/other-session&&relay send hermes:default/parent-session",
    "relay send hermes:default/other-session||relay send hermes:default/parent-session",
    "relay send hermes:default/other-session|relay send hermes:default/parent-session",
    "relay send hermes:default/other-session\nrelay send hermes:default/parent-session",
    "(relay send hermes:default/parent-session)",
    "if true; then relay send hermes:default/parent-session; fi",
    "X=1 /usr/local/bin/relay --json send hermes:default/parent-session",
    "env X=1 relay send hermes:default/parent-session",
    "command relay send hermes:default/parent-session",
    "relay --fields address send --steer hermes:default/parent-session",
    "relay send --body-file=result.txt hermes:default/parent-session",
    "relay send -- hermes:default/parent-session",
    "relay send hermes:default/parent-session&&printf done",
    "relay send hermes:default/parent-session||printf done",
    "relay send hermes:default/parent-session& printf done",
    "relay send hermes:default/parent-session&>/dev/null",
    "relay send 2>/dev/null hermes:default/parent-session",
    "</dev/null relay send hermes:default/parent-session",
    "relay \\\n send hermes:default/parent-session",
    "relay send hermes:default/parent-\\\nsession",
    "relay send hermes:default/other-session <<EOF\nHere's result\nEOF\nrelay send hermes:default/parent-session",
    "relay send hermes:default/parent-session <<-'EOF'\n\tHere's result\n\tEOF",
    "relay send hermes:default/other-session <<EOF\n`relay send hermes:default/parent-session`\nEOF",
    "relay send hermes:default/other-session <<EOF\n$(printf done\nrelay send hermes:default/parent-session)\nEOF",
    "cat <<EOF\n$(relay send hermes:default/parent-session)\nEOF",
])
def test_relay_cli_each_shell_invocation_is_guarded(command, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = _bind("parent-session")
    try:
        with delegated_child_context("child-session"):
            assert cli_child_parent_relay_error(command) == CHILD_PARENT_RELAY_ERROR
        assert cli_child_parent_relay_error(command) is None
    finally:
        session_context.clear_session_vars(tokens)


@pytest.mark.parametrize("command", [
    "relay send hermes:default/other-session 'unterminated",
    "relay --fields address send 'unterminated",
    "relay send hermes:default/other-session <<EOF\nunterminated",
    "relay send \"$DESTINATION\"",
    "relay send $(printf hermes:default/parent-session)",
    "relay send --unknown-option hermes:default/parent-session",
    "relay send hermes:default/other-session <<EOF\n$(relay send hermes:default/parent-session)\nEOF",
])
def test_ambiguous_relay_cli_is_denied_only_for_child(command, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = _bind("parent-session")
    try:
        with delegated_child_context("child-session"):
            assert cli_child_parent_relay_error(command) == CHILD_PARENT_RELAY_ERROR
        assert cli_child_parent_relay_error(command) is None
    finally:
        session_context.clear_session_vars(tokens)


@pytest.mark.parametrize("address, blocked", [
    ("hermes:default/parent-session", True),
    ("hermes:default/other-session", False),
])
def test_terminal_heredoc_guard_runs_before_execution(address, blocked, tmp_path, monkeypatch):
    from tools import terminal_tool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = _bind("parent-session")
    command = f"relay --json send {address} <<EOF\nHere's hermes:default/parent-session\nEOF"
    try:
        with delegated_child_context("child-session"), patch(
            "tools.terminal_tool.gateway_lifecycle_block", side_effect=RuntimeError("next guard reached")
        ):
            expected = terminal_tool._Rejected if blocked else RuntimeError
            with pytest.raises(expected) as caught:
                terminal_tool._pre_exec_block(
                    command, env={}, env_type="local", cwd=str(tmp_path), workdir=None, session_key="",
                )
            if blocked:
                assert CHILD_PARENT_RELAY_ERROR in caught.value.result_json
            else:
                assert str(caught.value) == "next guard reached"
    finally:
        session_context.clear_session_vars(tokens)


def test_heredoc_regression_is_valid_shell_and_passes_body_as_stdin():
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required for the real-shell heredoc probe")
        return
    command = (
        "relay --json send hermes:default/parent-session <<EOF\n"
        "📨 Result ready\nHere's the result.\nEOF"
    )
    # This local function is the complete transport substitute: no real relay
    # executable or remote side effect can be reached by this shell probe.
    probe = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", 'relay() { printf "%s\\n" "$3"; cat; };\n' + command],
        check=True, capture_output=True, text=True,
    )
    assert probe.stdout == "hermes:default/parent-session\n📨 Result ready\nHere's the result.\n"


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
