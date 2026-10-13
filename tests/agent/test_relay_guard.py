"""Delegated children must return parent-directed content through native completion."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.delegation_context import delegated_child_context, delegated_child_subprocess_env
from agent.relay_guard import (
    CHILD_PARENT_RELAY_ERROR,
    RELAY_PARENT_ADDRESS_ENV,
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


PARENT_ADDRESS = "hermes:default/parent-session"

# Stands in for relay's CLI check (relay C27): refuse a send whose target is
# RELAY_PARENT_ADDRESS. It records what reached it so the test sees both the
# env Hermes handed the process and the argv bash resolved.
_STUB_RELAY = """#!/bin/sh
printf '%s %s\\n' "${RELAY_PARENT_ADDRESS-<unset>}" "$2" >> "$RELAY_STUB_LOG"
if [ "$1" = send ] && [ -n "${RELAY_PARENT_ADDRESS-}" ] && [ "$2" = "$RELAY_PARENT_ADDRESS" ]; then
  echo "error: child_parent_relay: put this in your final result; the parent receives it when you finish" >&2
  exit 1
fi
cat >/dev/null
echo sent
"""

# Spellings that defeated the former shell-text parser. Each reaches relay
# with the parent as its argv destination.
SHELL_SPELLINGS = (
    "{ relay send hermes:default/parent-session; }",
    "relay send hermes:default/parent-session; printf done",
    "relay send hermes:default/parent-session|cat",
    "relay send hermes:default/parent-session>/dev/null",
    "relay send hermes:default/parent-session <<EOF\n📨 Result ready\nHere's the result.\nEOF",
    "relay send hermes:default/parent-session <<'EOF'\nDon't stop\nEOF",
    'a=hermes:default; relay send "$a/parent-session"',
    "eval 'relay send hermes:default/parent-session'",
)


def test_child_subprocess_env_names_parent_and_top_level_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(RELAY_PARENT_ADDRESS_ENV, raising=False)
    tokens = _bind("parent-session")
    try:
        assert RELAY_PARENT_ADDRESS_ENV not in delegated_child_subprocess_env({})
        with delegated_child_context("child-session"):
            assert delegated_child_subprocess_env({})[RELAY_PARENT_ADDRESS_ENV] == PARENT_ADDRESS
            with delegated_child_context("grandchild-session"):
                # A nested child names its immediate parent.
                assert delegated_child_subprocess_env({})[RELAY_PARENT_ADDRESS_ENV] == "hermes:default/child-session"
        assert RELAY_PARENT_ADDRESS_ENV not in delegated_child_subprocess_env({"PATH": "/bin"})
    finally:
        session_context.clear_session_vars(tokens)


def test_child_spawn_surfaces_carry_parent_address(tmp_path, monkeypatch):
    from tools.code_execution_env import _scrub_child_env
    from tools.environments.local import _make_run_env
    from tools.process_registry import ProcessRegistry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(RELAY_PARENT_ADDRESS_ENV, raising=False)
    tokens = _bind("parent-session")
    try:
        surfaces = {
            "terminal": lambda: _make_run_env({}),
            "background": lambda: ProcessRegistry._spawn_env({}),
            "execute_code": lambda: _scrub_child_env({"PATH": "/bin"}),
        }
        for name, build in surfaces.items():
            assert RELAY_PARENT_ADDRESS_ENV not in build(), name
            with delegated_child_context("child-session"):
                assert build().get(RELAY_PARENT_ADDRESS_ENV) == PARENT_ADDRESS, name
    finally:
        session_context.clear_session_vars(tokens)


@pytest.mark.platforms("posix")
def test_child_terminal_relay_to_parent_is_refused_for_every_shell_spelling(tmp_path, monkeypatch):
    from tools.environments.local import LocalEnvironment

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    relay = bin_dir / "relay"
    relay.write_text(_STUB_RELAY)
    relay.chmod(0o755)
    log = tmp_path / "relay.log"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("RELAY_STUB_LOG", str(log))
    monkeypatch.delenv(RELAY_PARENT_ADDRESS_ENV, raising=False)
    tokens = _bind("parent-session")
    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    env.init_session()
    try:
        with delegated_child_context("child-session"):
            for command in SHELL_SPELLINGS:
                out = env.execute(command)["output"]
                assert "child_parent_relay" in out, (command, out)
            assert "sent" in env.execute("relay send hermes:default/other-session </dev/null")["output"]
        # The child's identity never persists into the shared snapshot.
        assert "sent" in env.execute(SHELL_SPELLINGS[0] + " </dev/null")["output"]
    finally:
        env.cleanup()
        session_context.clear_session_vars(tokens)
    lines = log.read_text().splitlines()
    assert lines[:len(SHELL_SPELLINGS)] == [f"{PARENT_ADDRESS} {PARENT_ADDRESS}"] * len(SHELL_SPELLINGS)
    assert lines[-1] == f"<unset> {PARENT_ADDRESS}"


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
