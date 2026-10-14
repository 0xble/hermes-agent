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


@pytest.mark.parametrize("depth", [2, 3])
def test_descendant_mcp_and_cli_guard_the_lineage_topic(tmp_path, monkeypatch, depth):
    from contextlib import ExitStack
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(RELAY_PARENT_ADDRESS_ENV, raising=False)
    children = [f"child-{i}" for i in range(depth)]
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="old-topic", source="telegram")
        db.create_session(session_id="parent-session", source="telegram", parent_session_id="old-topic")
        parent = "parent-session"
        for child in children:
            db.create_session(session_id=child, source="subagent", parent_session_id=parent)
            parent = child
    finally:
        db.close()
    tokens = _bind("parent-session")
    send = mcp_tool_handlers._make_tool_handler("relay", "send", 30.0)
    try:
        with ExitStack() as stack:
            for child in children:
                stack.enter_context(delegated_child_context(child))
            with patch("tools.mcp_tool_handlers._acquire_call_server", side_effect=AssertionError("transport reached")):
                for target in (PARENT_ADDRESS, f"hermes:default/{children[-2]}"):
                    assert CHILD_PARENT_RELAY_ERROR in send({"address": target, "body": "do not send", "fyi": True})
            with patch("tools.mcp_tool_handlers._acquire_call_server", return_value=(None, "peer reached")):
                for target in ("hermes:default/peer", "hermes:default/old-topic", "hermes:other/parent-session"):
                    assert send({"address": target, "body": "allowed"}) == "peer reached"
            assert delegated_child_subprocess_env({})[RELAY_PARENT_ADDRESS_ENV] == PARENT_ADDRESS
            # The in-process child names its own guard over an inherited outer value,
            # matching the terminal wrapper's export.
            inherited = {RELAY_PARENT_ADDRESS_ENV: "hermes:default/inherited"}
            assert delegated_child_subprocess_env(inherited)[RELAY_PARENT_ADDRESS_ENV] == PARENT_ADDRESS
    finally:
        session_context.clear_session_vars(tokens)


@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("surface", ["tui", "desktop", "telegram"])
def test_surface_labelled_child_rows_do_not_change_the_guarded_topic(tmp_path, monkeypatch, surface, depth):
    # TUI and desktop child rows inherit the surface source instead of "subagent", so
    # session rows cannot tell a nested child from a topic. The topic comes from context.
    from contextlib import ExitStack
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(RELAY_PARENT_ADDRESS_ENV, raising=False)
    children = [f"child-{i}" for i in range(depth)]
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="parent-session", source=surface)
        parent = "parent-session"
        for child in children:
            db.create_session(session_id=child, source=surface, parent_session_id=parent)
            parent = child
    finally:
        db.close()
    tokens = _bind("parent-session")
    send = mcp_tool_handlers._make_tool_handler("relay", "send", 30.0)
    try:
        with ExitStack() as stack:
            for child in children:
                stack.enter_context(delegated_child_context(child))
            assert delegated_child_subprocess_env({})[RELAY_PARENT_ADDRESS_ENV] == PARENT_ADDRESS
            with patch("tools.mcp_tool_handlers._acquire_call_server", side_effect=AssertionError("transport reached")):
                assert CHILD_PARENT_RELAY_ERROR in send({"address": PARENT_ADDRESS, "body": "do not send"})
    finally:
        session_context.clear_session_vars(tokens)


def test_topic_guard_needs_no_session_database(tmp_path, monkeypatch):
    # No state.db exists: the guard still refuses the topic and the immediate parent.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(RELAY_PARENT_ADDRESS_ENV, raising=False)
    tokens = _bind("parent-session")
    try:
        with delegated_child_context("child-session"), delegated_child_context("grandchild-session"):
            assert child_parent_relay_error(PARENT_ADDRESS) == CHILD_PARENT_RELAY_ERROR
            assert child_parent_relay_error("hermes:default/child-session") == CHILD_PARENT_RELAY_ERROR
            assert child_parent_relay_error("hermes:default/peer") is None
            assert delegated_child_subprocess_env({})[RELAY_PARENT_ADDRESS_ENV] == PARENT_ADDRESS
        assert child_parent_relay_error(PARENT_ADDRESS) is None
    finally:
        session_context.clear_session_vars(tokens)
    assert not (tmp_path / "state.db").exists()


def test_chain_root_survives_a_child_worker_thread_without_the_parent_context(tmp_path, monkeypatch):
    # Real child runs start in executor threads, and a background child's thread does not
    # inherit the delegating turn's context. The root is stamped on each child agent at
    # build time and re-entered by the run, so a grandchild still guards the topic.
    import threading
    from unittest.mock import MagicMock
    from tools.delegate_tool import _build_child_agent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(RELAY_PARENT_ADDRESS_ENV, raising=False)

    def build(parent):
        with patch("run_agent.AIAgent") as agent_cls:
            agent_cls.return_value = MagicMock()
            return _build_child_agent(
                task_index=0, goal="g", context=None, toolsets=None, model=None,
                max_iterations=1, parent_agent=parent, task_count=1,
            )

    def parent_agent(session_id, depth, root=None):
        parent = MagicMock(spec_set=None)
        parent.session_id, parent._delegate_depth = session_id, depth
        parent._session_db, parent._active_children = None, []
        parent._active_children_lock = threading.Lock()
        parent.base_url, parent.provider, parent.model = "https://example.invalid/v1", "openrouter", "m"
        parent.api_key, parent.api_mode, parent.platform = "***", "chat_completions", "cli"
        parent.providers_allowed = parent.providers_ignored = parent.providers_order = None
        parent.provider_sort = parent._print_fn = parent.tool_progress_callback = None
        parent.thinking_callback = None
        if root is None:
            del parent._delegation_root_session_id
        else:
            parent._delegation_root_session_id = root
        return parent

    child = build(parent_agent("parent-session", 0))
    assert child._delegation_root_session_id == "parent-session"
    grandchild = build(parent_agent("child-session", 1, root=child._delegation_root_session_id))
    assert grandchild._delegation_root_session_id == "parent-session"

    seen = {}

    def run_grandchild():  # a fresh thread: no delegation or session context inherited
        tokens = _bind("child-session")
        try:
            with delegated_child_context(
                "grandchild-session", parent_session_id="child-session",
                root_session_id=grandchild._delegation_root_session_id,
            ):
                seen["topic"] = child_parent_relay_error(PARENT_ADDRESS)
                seen["parent"] = child_parent_relay_error("hermes:default/child-session")
                seen["env"] = delegated_child_subprocess_env({})[RELAY_PARENT_ADDRESS_ENV]
        finally:
            session_context.clear_session_vars(tokens)

    worker = threading.Thread(target=run_grandchild)
    worker.start()
    worker.join(10)
    assert seen == {
        "topic": CHILD_PARENT_RELAY_ERROR, "parent": CHILD_PARENT_RELAY_ERROR, "env": PARENT_ADDRESS,
    }


def test_child_run_enters_the_stamped_chain_root_on_its_worker_thread(tmp_path, monkeypatch):
    # The real run path, not a hand-built context: _run_single_child runs the conversation on a
    # worker thread, and the guard there must name the root stamped on the child.
    from unittest.mock import MagicMock
    from tools.delegate_tool import _run_single_child

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(RELAY_PARENT_ADDRESS_ENV, raising=False)
    seen = {}

    def run_conversation(**_kwargs):
        seen["topic"] = child_parent_relay_error(PARENT_ADDRESS)
        seen["env"] = delegated_child_subprocess_env({})[RELAY_PARENT_ADDRESS_ENV]
        return {"final_response": "done", "completed": True, "interrupted": False, "api_calls": 1, "messages": []}

    child = MagicMock()
    child.session_id, child._parent_session_id = "grandchild-session", "child-session"
    child._delegation_root_session_id = "parent-session"
    child._credential_pool = None
    child.run_conversation.side_effect = run_conversation
    parent = MagicMock()
    parent.session_id, parent._delegate_depth, parent._session_db = "child-session", 1, None
    parent._active_children, parent._print_fn, parent.tool_progress_callback = [], None, None
    import threading
    parent._active_children_lock = threading.Lock()
    tokens = _bind("child-session")
    try:
        result = _run_single_child(task_index=0, goal="g", child=child, parent_agent=parent)
    finally:
        session_context.clear_session_vars(tokens)
    assert result["status"] == "completed", result
    assert seen == {"topic": CHILD_PARENT_RELAY_ERROR, "env": PARENT_ADDRESS}


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
                # Relay's CLI takes one address; a nested child names the chain root (the
                # topic), since a subagent session has no topic of its own to relay into.
                # MCP still refuses the immediate parent too.
                assert delegated_child_subprocess_env({})[RELAY_PARENT_ADDRESS_ENV] == PARENT_ADDRESS
                assert child_parent_relay_error("hermes:default/child-session") == CHILD_PARENT_RELAY_ERROR
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
    # A login shell may reset PATH from /etc/profile (Debian/Ubuntu CI), so put
    # the stub on the session's own PATH, which the snapshot persists.
    env.execute(f'export PATH="{bin_dir}:$PATH"')
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


def _docker_env_capturing(calls):
    from tools.environments import docker as docker_env

    env = docker_env.DockerEnvironment.__new__(docker_env.DockerEnvironment)
    env.cwd, env.timeout, env._forward_env, env._env = "/root", 60, [], {}
    env._prepare_command = lambda command: (command, None)
    env._container_id, env._docker_exe = "test-container", "/usr/bin/docker"
    env._session_id, env._cwd_marker = "t1", "__HERMES_CWD_t1__"
    env._snapshot_path = "/tmp/hermes-snap-t1.sh"
    env._snapshot_ready, env._prefer_nonlogin = True, False
    env._snapshot_passthrough_names, env._init_env_args = set(), []
    env._persist_across_processes = False
    env._wait_for_process = lambda proc, **_: {"output": "", "returncode": 0}
    return env


def _ssh_env_capturing(monkeypatch):
    from tools.environments import ssh as ssh_env

    env = ssh_env.SSHEnvironment.__new__(ssh_env.SSHEnvironment)
    env.host, env.user, env.port, env.key_path = "h", "u", 22, ""
    env.cwd, env.timeout = "~", 60
    env.control_socket = __import__("pathlib").Path("/tmp/hermes-ssh-test/x.sock")
    env._session_id, env._cwd_marker = "t2", "__HERMES_CWD_t2__"
    env._snapshot_path = "/tmp/hermes-snap-t2.sh"
    env._snapshot_ready, env._prefer_nonlogin = True, False
    env._snapshot_passthrough_names, env._sync_manager = set(), None
    env._wait_for_process = lambda proc, **_: {"output": "", "returncode": 0}
    return env


@pytest.mark.parametrize("backend", ["docker", "ssh"])
def test_remote_backend_commands_carry_parent_address_only_for_children(tmp_path, monkeypatch, backend):
    """Remote backends forward no host env, so the shared command wrapper carries
    the address. It is exported after the snapshot is sourced and unset before the
    re-dump, so a later top-level command on the same backend never inherits it."""
    import shlex

    from tools.environments import docker as docker_env, ssh as ssh_env

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(RELAY_PARENT_ADDRESS_ENV, raising=False)
    calls: list[list[str]] = []
    module = docker_env if backend == "docker" else ssh_env
    monkeypatch.setattr(module, "_popen_bash", lambda cmd, stdin_data=None, **kw: calls.append(cmd) or object())
    env = _docker_env_capturing(calls) if backend == "docker" else _ssh_env_capturing(monkeypatch)
    tokens = _bind("parent-session")
    try:
        env.execute("relay send hermes:default/parent-session")
        with delegated_child_context("child-session"):
            env.execute("relay send hermes:default/parent-session")
    finally:
        session_context.clear_session_vars(tokens)

    def script(cmd):
        # ssh quotes the script once more for the remote login shell.
        return shlex.split(cmd[-1])[-1] if backend == "ssh" else cmd[-1]

    top_level, child = map(script, calls)
    export = f"export {RELAY_PARENT_ADDRESS_ENV}={PARENT_ADDRESS}"
    assert RELAY_PARENT_ADDRESS_ENV + "=" not in top_level
    assert export in child.splitlines()
    lines = child.splitlines()
    assert lines.index(export) > next(i for i, ln in enumerate(lines) if ln.startswith("source "))
    assert lines.index(export) < next(i for i, ln in enumerate(lines) if ln.startswith("eval "))


def test_wrapper_quotes_parent_address_and_keeps_it_out_of_snapshot(tmp_path):
    """Real bash: the command sees the address verbatim; the re-dumped snapshot does not carry it."""
    import subprocess

    from tools.environments.base_session_env import _wrap_command_script

    snap = tmp_path / "snap.sh"
    snap.write_text("")
    hostile = "hermes:default/p'; touch pwned; '"
    script = _wrap_command_script(
        f'printf "%s" "${RELAY_PARENT_ADDRESS_ENV}"', quoted_cwd=str(tmp_path), quoted_snap=str(snap),
        snap_tmp_template=f"{snap}.tmp.XXXXXX", passthrough_names=(), snapshot_ready=True,
        cwd_marker="__M__", relay_parent_address=hostile)
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=tmp_path,
                         env={"PATH": "/usr/bin:/bin"}).stdout
    assert out.startswith(hostile)
    assert not (tmp_path / "pwned").exists()
    assert RELAY_PARENT_ADDRESS_ENV not in snap.read_text()


def test_managed_modal_exec_payload_carries_parent_address_only_for_children(tmp_path, monkeypatch):
    from tools.environments.managed_modal import ManagedModalEnvironment

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    env = ManagedModalEnvironment.__new__(ManagedModalEnvironment)
    env.cwd, env.timeout, env._sandbox_id = "/root", 60, "sb"
    env._prepare_command = lambda command: (command, None)
    payloads = []

    class _Resp:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def _request(method, path, json=None, timeout=None):
        payloads.append(json)
        return _Resp({"status": "completed", "output": "", "returncode": 0})

    env._request = _request
    tokens = _bind("parent-session")
    try:
        env.execute("true")
        with delegated_child_context("child-session"):
            env.execute("true")
    finally:
        session_context.clear_session_vars(tokens)
    assert RELAY_PARENT_ADDRESS_ENV not in payloads[0]["command"]
    assert payloads[1]["command"].startswith(f"export {RELAY_PARENT_ADDRESS_ENV}={PARENT_ADDRESS}\n")
