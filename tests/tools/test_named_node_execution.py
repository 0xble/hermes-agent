"""Named-node integration: real dispatch/cache/SSH shell/file/process layers.

Only the SSH transport is substituted. Commands execute in fixture-owned peer
namespaces on the actual controller OS, never by pretending it is Linux.
"""
import json
import base64
import re
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tools import file_tools as ft, process_registry as pr, terminal_tool as tt
from tools.environments import ssh


@pytest.fixture
def peers(monkeypatch, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps({"nodes": {
        "default": "macstudio", "hosts": {
            name: {"ssh": name, "platform": "macos"} for name in ("macstudio", "macbook")}}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Nothing here rewrites sys.platform or os.name.
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(ft, "_start_cleanup_thread", lambda: None, raising=False)
    tt._active_environments.clear()
    tt._session_cwd.clear()
    ft._file_ops_cache.clear()
    tt._task_env_overrides.clear()
    ft.clear_file_ops_cache()
    roots = {name: tmp_path / name for name in ("macstudio", "macbook")}
    logical = str(tmp_path / "shared")
    for root in roots.values():
        (root / "home").mkdir(parents=True)
        (root / "tmp").mkdir()
        (root / "home" / "sub").mkdir()
        (root / "same.txt").write_text(root.name + "\nsecond\n")
    offline = set()
    calls = []

    def prepare(env, command):
        calls.append((env.host, command))
        if env.host in offline:
            raise OSError("fixture peer unavailable")
        assert env.host in roots, "fallback or sibling transport"
        assert env.user == "" and getattr(env, "_sync_manager", None) is None
        root = roots[env.host]
        def translate_encoded(match):
            try:
                source = base64.b64decode(match.group()).decode()
            except (ValueError, UnicodeError):
                return match.group()
            source = source.replace(logical, str(root))
            return base64.b64encode(source.encode()).decode()
        command = re.sub(r"[A-Za-z0-9+/=]{80,}", translate_encoded, command)
        command = command.replace(logical, str(root))
        # Keep target-side snapshots and receipts in owned fixture directories.
        for prefix in ("hermes-snap-", "hermes-cwd-"):
            command = command.replace("/tmp/" + prefix, str(root / "tmp" / prefix))
        # A synthetic target owns a supported Python, not the controller's user
        # shims. Recovered handles invoke helpers before a terminal snapshot.
        return command, {**os.environ, "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
                         "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath}

    def run_ssh(env, remote_cmd, timeout):
        command, variables = prepare(env, remote_cmd)
        return subprocess.run(["bash", "-c", command], env=variables, text=True,
                              capture_output=True, timeout=timeout, check=False)

    def run_bash(env, cmd_string, *, login=False, timeout=120, stdin_data=None):
        command, variables = prepare(env, cmd_string)
        # Non-login fixture shells cannot source the controller's startup files.
        proc = subprocess.Popen(["bash", "-c", command], env=variables, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if stdin_data is not None:
            proc.stdin.write(stdin_data)
        proc.stdin.close()
        proc.stdin = None
        return proc

    monkeypatch.setattr(ssh.SSHEnvironment, "_run_ssh", run_ssh)
    monkeypatch.setattr(ssh.SSHEnvironment, "_run_bash", run_bash)
    monkeypatch.setattr(ssh, "_ensure_ssh_available", lambda: None)
    process_registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", process_registry)
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    monkeypatch.setattr(process_registry, "_start_ssh_poller", lambda session: None)
    yield roots, logical, offline, calls, process_registry, home
    for session in list(process_registry._running.values()):
        offline.clear()
        if not session.exited:
            process_registry._kill_ssh(session)
    process_registry._survivor_pollers_stopped.set()
    for env in tt._active_environments.values():
        # No real SSH ControlMaster exists in this transport.
        assert env._sync_manager is None
    tt._active_environments.clear()
    tt._session_cwd.clear()
    ft._file_ops_cache.clear()
    tt._task_env_overrides.clear()
    ft.clear_file_ops_cache()


def call(name, args, *, task="raw-owner", session="chat"):
    from tools.approval_context import set_current_session_key, reset_current_session_key
    from model_tools import handle_function_call
    token = set_current_session_key(session)
    try:
        return json.loads(handle_function_call(name, args, task_id=task, session_id=session))
    finally:
        reset_current_session_key(token)


def test_alternating_terminal_and_files_keep_cwd_exports_and_baselines(peers):
    roots, path, *_ = peers
    first = call("terminal", {"command": "cd ~/sub; export NODE_ONLY=studio; pwd"})
    assert first["node"] == "macstudio" and first["exit_code"] == 0, first
    other = call("terminal", {"node": "macbook", "command": "printf '%s' \"${NODE_ONLY-unset}\"; pwd"})
    assert "unset" in other["output"] and str(roots["macbook"] / "home") in other["output"], other
    again = call("terminal", {"node": "macstudio", "command": "printf '%s' \"$NODE_ONLY\"; pwd"})
    assert "studio" in again["output"] and str(roots["macstudio"] / "home/sub") in again["output"], again
    for node in roots:
        result = call("read_file", {"node": node, "path": path + "/same.txt"})
        assert node in result["content"] and result["node"] == node, result
    changed = call("write_file", {"node": "macstudio", "path": path + "/same.txt", "content": "studio changed\n"})
    assert changed.get("verified"), changed
    assert (roots["macbook"] / "same.txt").read_text() == "macbook\nsecond\n"
    patched = call("patch", {"node": "macbook", "path": path + "/same.txt", "old_string": "macbook", "new_string": "book changed"})
    assert "error" not in patched, patched
    assert (roots["macstudio"] / "same.txt").read_text() == "studio changed\n"
    search = call("search_files", {"node": "macbook", "pattern": "book changed", "path": path})
    assert "same.txt" in str(search) and search["node"] == "macbook", search
    after_files = call("terminal", {"node": "macstudio", "command": 'printf "%s:%s" "$PWD" "$NODE_ONLY"'})
    assert after_files["output"] == f"{roots['macstudio']}/home/sub:studio", after_files
    assert all(getattr(env, "_sync_manager", None) is None for env in tt._active_environments.values())


def test_read_authority_does_not_cross_node_session_or_partial_read(peers):
    _, path, *_ = peers
    assert "content" in call("read_file", {"node": "macstudio", "path": path + "/same.txt"})
    wrong_node = call("write_file", {"node": "macbook", "path": path + "/same.txt", "content": "not authorized"})
    assert "error" in wrong_node, wrong_node
    wrong_session = call("write_file", {"node": "macstudio", "path": path + "/same.txt", "content": "not authorized"}, session="other-chat")
    assert "error" in wrong_session, wrong_session
    read = call("read_file", {"node": "macbook", "path": path + "/same.txt", "offset": 1, "limit": 1})
    assert read["truncated"], read
    partial = call("write_file", {"node": "macbook", "path": path + "/same.txt", "content": "partial"})
    assert "error" in partial, partial
    assert "content" in call("read_file", {"node": "macbook", "path": path + "/same.txt", "offset": 2})
    result = call("write_file", {"node": "macbook", "path": path + "/same.txt", "content": "covered"})
    assert result.get("verified"), result


def test_remote_missing_controller_twin_and_unavailable_never_fall_back(peers):
    roots, path, offline, calls, *_ = peers
    twin = Path(path)
    twin.mkdir()
    (twin / "missing.txt").write_text("controller only")
    absent = call("read_file", {"node": "macstudio", "path": path + "/missing.txt"})
    assert "error" in absent and "controller only" not in str(absent), absent
    # A cached miss stays remote even when a local twin exists.
    absent_again = call("read_file", {"node": "macstudio", "path": path + "/missing.txt"})
    assert "error" in absent_again, absent_again
    created = call("write_file", {"node": "macstudio", "path": path + "/missing.txt", "content": "remote new"})
    assert created.get("verified"), created
    assert (twin / "missing.txt").read_text() == "controller only"
    offline.add("macbook")
    error = call("terminal", {"node": "macbook", "command": "printf MUST_NOT_FALL_BACK"})
    assert "error" in error and error["node"] == "macbook", error
    before = len(calls)
    unknown = call("read_file", {"node": "unknown", "path": path + "/same.txt"})
    assert "error" in unknown and len(calls) == before, unknown
    pty = call("terminal", {"node": "macstudio", "command": "true", "pty": True})
    assert "error" in pty and "PTY" in pty["error"] and len(calls) == before
    assert not (roots["macbook"] / "missing.txt").exists()


def test_concurrent_sessions_get_independent_shell_snapshots(peers):
    def one(pair):
        node, session = pair
        value = node + session
        result = call("terminal", {"node": node, "command": f"export SCOPED={shlex.quote(value)}; cd ~/sub"}, session=session)
        assert result["exit_code"] == 0, result
        result = call("terminal", {"node": node, "command": "printf '%s' \"$SCOPED\""}, session=session)
        assert result["output"].strip() == value, result
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, [(n, s) for n in ("macstudio", "macbook") for s in ("session-a", "session-b")]))
    assert len(tt._active_environments) == 4


def test_file_cwd_refresh_stale_hash_and_patch_provenance(peers):
    roots, path, *_ = peers
    for node in roots:
        result = call("read_file", {"node": node, "path": path + "/same.txt"})
        assert "content" in result, result
    result = call("terminal", {"node": "macstudio", "command": "cd ~/sub"})
    assert result["exit_code"] == 0, result
    result = call("write_file", {"node": "macstudio", "path": "relative.txt", "content": "scoped cwd"})
    assert result.get("verified"), result
    assert (roots["macstudio"] / "home/sub/relative.txt").read_text() == "scoped cwd"
    assert not (roots["macbook"] / "home/sub/relative.txt").exists()
    target = roots["macstudio"] / "same.txt"
    before = target.stat()
    target.write_text("externally modified\n")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    stale = call("write_file", {"node": "macstudio", "path": path + "/same.txt", "content": "stale view"})
    assert stale.get("stale_write_blocked"), stale
    assert target.read_text() == "externally modified\n"
    # A blind targeted patch is supported, but does not confer full-overwrite authority.
    result = call("patch", {"node": "macstudio", "path": path + "/same.txt", "old_string": "externally", "new_string": "targeted"})
    assert "error" not in result, result
    result = call("write_file", {"node": "macstudio", "path": path + "/same.txt", "content": "still blind"})
    assert result.get("stale_write_blocked"), result


def test_concurrent_profiles_and_raw_task_cleanup(peers, tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.node_execution import current_execution
    from tools.terminal_tool_lifecycle import cleanup_vm
    roots, path, _, _, _, home = peers
    other_home = tmp_path / "profile-b"
    other_home.mkdir()
    (other_home / "config.yaml").write_text((home / "config.yaml").read_text())
    def one(profile):
        token = set_hermes_home_override(profile)
        try:
            value = profile.name
            result = call("terminal", {"node": "macstudio", "command": f"export PROFILE_VALUE={value}"})
            assert result["exit_code"] == 0, result
            result = call("read_file", {"node": "macstudio", "path": path + "/same.txt"})
            assert "content" in result, result
            result = call("terminal", {"node": "macstudio", "command": "printf '%s' \"$PROFILE_VALUE\""})
            assert result["output"].strip() == value, result
            assert current_execution() is None
        finally:
            reset_hermes_home_override(token)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(one, (home, other_home)))
    assert len(tt._active_environments) == 2
    # No live sockets in the fixture, so teardown must never invoke host SSH.
    for env in tt._active_environments.values():
        env._control_sockets = list
    cleanup_vm("raw-owner")
    assert len(tt._active_environments) == 1
    survivor = next(iter(tt._active_environments.values()))
    assert survivor._hermes_profile_home == str(other_home)
    token = set_hermes_home_override(other_home)
    try:
        result = call("write_file", {"node": "macstudio", "path": path + "/same.txt", "content": "profile b baseline"})
        assert result.get("verified"), result
        cleanup_vm("raw-owner")
        assert not tt._active_environments
    finally:
        reset_hermes_home_override(token)
    assert (roots["macstudio"] / "same.txt").read_text() == "profile b baseline"


def test_denied_and_conflicting_scope_refuses_before_transport(peers):
    from tools.terminal_scope import TerminalPolicyRefusal, set_terminal_scope, reset_terminal_scope
    _, path, _, calls, *_ = peers
    token = set_terminal_scope(TerminalPolicyRefusal("fixture denied profile"))
    try:
        for name, args in (("terminal", {"command": "true"}), ("read_file", {"path": path + "/same.txt"}), ("process", {"action": "list"})):
            result = call(name, {**args, "node": "macbook"})
            assert "fixture denied profile" in result["error"] and result["node"] == "macbook", result
    finally:
        reset_terminal_scope(token)
    assert not calls
    tt.register_task_env_overrides("raw-owner", {"env_type": "docker"})
    result = call("terminal", {"node": "macbook", "command": "true"})
    assert "conflict" in result["error"].lower() and not calls, result


def test_no_node_configuration_retains_local_public_behavior(peers, monkeypatch, tmp_path):
    _, _, _, calls, _, home = peers
    (home / "config.yaml").write_text("{}")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    local = tmp_path / "local.txt"
    local.write_text("legacy local")
    result = call("read_file", {"path": str(local)})
    assert "legacy local" in result["content"] and "node" not in result, result
    result = call("write_file", {"path": str(local), "content": "legacy edited"})
    assert result.get("verified") and "node" not in result, result
    assert local.read_text() == "legacy edited" and not calls
    # The fixture's teardown assertions only apply to named-node SSH envs.
    tt._active_environments.clear()


def test_uncertain_terminal_transport_does_not_replay(peers, monkeypatch):
    from tools.environments.base import EnvironmentConnectionError
    roots, path, *_ = peers
    assert call("terminal", {"node": "macstudio", "command": "true"})["exit_code"] == 0
    original = ssh.SSHEnvironment._run_bash
    executions = []
    def drop_after_dispatch(env, command, **kwargs):
        executions.append(command)
        proc = original(env, command, **kwargs)
        proc.wait(timeout=10)
        raise EnvironmentConnectionError("fixture transport lost after execution")
    monkeypatch.setattr(ssh.SSHEnvironment, "_run_bash", drop_after_dispatch)
    result = call("terminal", {"node": "macstudio", "command": f"printf once >> {shlex.quote(path + '/once.txt')}"})
    assert result.get("error") and result["node"] == "macstudio", result
    assert len(executions) == 1 and (roots["macstudio"] / "once.txt").read_text() == "once"
    assert not (roots["macbook"] / "once.txt").exists()


def test_uncertain_background_public_handle_is_recoverable_without_replay(peers, monkeypatch):
    from tools.environments.base import EnvironmentConnectionError
    roots, path, *_ = peers
    assert call("terminal", {"node": "macbook", "command": "true"})["exit_code"] == 0
    original = ssh.SSHEnvironment._run_ssh
    launches = []
    def drop_launch_response(env, command, timeout):
        result = original(env, command, timeout)
        if not launches:
            launches.append(command)
            raise EnvironmentConnectionError("fixture lost launch response")
        return result
    monkeypatch.setattr(ssh.SSHEnvironment, "_run_ssh", drop_launch_response)
    result = call("terminal", {"node": "macbook", "command": f"printf once >> {shlex.quote(path + '/launch.txt')}; exec sleep 60", "background": True})
    assert result.get("session_id") and result["node"] == "macbook", result
    handle = result["session_id"]
    result = call("process", {"action": "poll", "session_id": handle})
    assert result.get("status") == "running", result
    assert len(launches) == 1
    result = call("process", {"action": "kill", "session_id": handle})
    assert result["status"] == "killed", result
    assert (roots["macbook"] / "launch.txt").read_text() == "once"
    assert not (roots["macstudio"] / "launch.txt").exists()


def test_unverified_named_write_fails_without_authorizing_rewrite(peers, monkeypatch):
    from tools.file_operations import ShellFileOperations
    roots, path, *_ = peers
    result = call("read_file", {"node": "macstudio", "path": path + "/same.txt"})
    assert "content" in result, result
    # Fault injection at the verification seam only. All dispatch, guards,
    # provenance, metadata and actual target writes still run normally.
    monkeypatch.setattr(ShellFileOperations, "_verify_written_hash", lambda *a: (None, None))
    result = call("write_file", {"node": "macstudio", "path": path + "/same.txt", "content": "written but unverified"})
    assert "unverified" in result.get("error", ""), result
    assert (roots["macstudio"] / "same.txt").read_text() == "written but unverified"
    result = call("write_file", {"node": "macstudio", "path": path + "/same.txt", "content": "must re-read"})
    assert result.get("stale_write_blocked"), result


def test_no_selected_path_probes_on_controller(peers, monkeypatch):
    import builtins
    from tools.node_execution import current_execution
    roots, path, *_ = peers
    target = roots["macstudio"] / "config.yaml"
    target.write_text("value: plain\n")
    original_open, original_stat, original_lstat, original_readlink = builtins.open, os.stat, os.lstat, os.readlink
    def checked(fn):
        def run(candidate, *args, **kwargs):
            if current_execution() and isinstance(candidate, (str, bytes, os.PathLike)):
                text = os.fsdecode(candidate)
                if text.startswith(path) or any(text.startswith(str(root)) for root in roots.values()):
                    pytest.fail("Selected-node path reached controller filesystem: " + text)
            return fn(candidate, *args, **kwargs)
        return run
    monkeypatch.setattr(builtins, "open", checked(original_open))
    monkeypatch.setattr(os, "stat", checked(original_stat))
    monkeypatch.setattr(os, "lstat", checked(original_lstat))
    monkeypatch.setattr(os, "readlink", checked(original_readlink))
    result = call("read_file", {"node": "macstudio", "path": path + "/config.yaml"})
    assert "value: plain" in result.get("content", ""), result
    result = call("write_file", {"node": "macstudio", "path": path + "/config.yaml", "content": "value: changed\n"})
    assert result.get("verified"), result
    result = call("patch", {"node": "macstudio", "path": path + "/config.yaml", "old_string": "changed", "new_string": "patched"})
    assert "error" not in result, result
    result = call("read_file", {"node": "macbook", "path": path + "/missing.txt"})
    assert "error" in result, result


def test_named_background_inherits_snapshot_and_reports_completion(peers):
    result = call("terminal", {"node": "macbook", "command": "export FROM_SNAPSHOT=book; cd ~/sub"})
    assert result["exit_code"] == 0, result
    spawned = call("terminal", {"node": "macbook", "command": "printf '%s' \"$FROM_SNAPSHOT\"; pwd; exit 7", "background": True, "notify": True})
    assert spawned.get("session_id"), spawned
    result = call("process", {"action": "wait", "session_id": spawned["session_id"], "timeout": 10})
    assert result["node"] == "macbook" and result["exit_code"] == 7 and "book" in result["output"], result
    assert "home/sub" in result["output"], result
    session = peers[4].get(spawned["session_id"])
    assert session.session_key == "chat" and session.owner_task_id == "raw-owner"


def test_background_public_dispatch_kill_and_checkpoint_recovery(peers, monkeypatch):
    _roots, _, offline, _, processes, _ = peers
    spawned = call("terminal", {"node": "macstudio", "command": "printf ready; exec sleep 60", "background": True})
    assert spawned["node"] == "macstudio" and spawned["session_id"], spawned
    handle = spawned["session_id"]
    session = processes.get(handle)
    assert session.owner_task_id == "raw-owner" and session.pid_scope == "ssh" and session.session_key == "chat"
    mismatch = call("process", {"action": "kill", "node": "macbook", "session_id": handle})
    assert "error" in mismatch and not session.exited, mismatch
    config_path = peers[5] / "config.yaml"
    config = json.loads(config_path.read_text())
    config["nodes"]["hosts"]["macstudio"]["platform"] = "linux"
    config_path.write_text(json.dumps(config))
    mismatch = call("process", {"action": "poll", "node": "macstudio", "session_id": handle})
    assert "error" in mismatch and not session.exited, mismatch
    offline.add("macstudio")
    unavailable = call("process", {"action": "poll", "session_id": handle})
    assert unavailable.get("remote_status") == "unavailable" and not session.exited, unavailable
    offline.clear()
    processes._write_checkpoint()
    recovered = pr.ProcessRegistry()
    monkeypatch.setattr(recovered, "_start_ssh_poller", lambda session: None)
    monkeypatch.setattr(pr, "process_registry", recovered)
    monkeypatch.setattr(recovered, "_detached_host_fate", lambda *a: pytest.fail("remote PID reached local liveness"))
    assert recovered.recover_from_checkpoint() == 1
    restored = recovered.get(handle)
    assert restored.ssh_binding == session.ssh_binding and restored.owner_task_id == "raw-owner"
    killed = call("process", {"action": "kill", "session_id": handle})
    assert killed["node"] == "macstudio" and killed["status"] in ("killed", "already_exited"), killed
    assert restored.exited and restored.completion_reason == "killed"
    session.exited = True  # Original controller instance no longer owns the fixture process.
    recovered._survivor_pollers_stopped.set()


@pytest.mark.parametrize("value", [None, "", False, 1])
def test_invalid_node_selector_never_uses_default(peers, value):
    _, _, _, transports, _, _ = peers
    before = len(transports)
    result = call("terminal", {"command": "printf should-not-run", "node": value})
    assert "non-empty" in result["error"]
    assert len(transports) == before


def test_profiles_and_sessions_concurrently_keep_independent_state(peers):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.terminal_scope import set_terminal_scope, reset_terminal_scope
    roots, _, _, transports, registry, home = peers
    homes = [home.parent / f"profile-{i}" for i in range(2)]
    for profile in homes:
        profile.mkdir()
        (profile / "config.yaml").write_text((home / "config.yaml").read_text())
    barrier = Barrier(4)

    def worker(i):
        profile, session = homes[i // 2], f"session-{i % 2}"
        profile_token = set_hermes_home_override(profile)
        policy_token = set_terminal_scope({"TERMINAL_ENV": "local"})
        try:
            result = call("terminal", {"node": "macstudio", "command": f"mkdir -p ~/work{i}; cd ~/work{i}; export S5_STATE={i}"}, session=session)
            assert result["exit_code"] == 0, result
            barrier.wait(timeout=30)
            result = call("terminal", {"node": "macstudio", "command": 'printf "%s:%s" "$PWD" "$S5_STATE"'}, session=session)
            assert result["output"] == f"{roots['macstudio']}/home/work{i}:{i}", result
            launched = call("terminal", {"node": "macstudio", "command": "exec sleep 30", "background": True}, session=session)
            return profile, launched["session_id"]
        finally:
            reset_terminal_scope(policy_token)
            reset_hermes_home_override(profile_token)

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            handles = list(pool.map(worker, range(4)))
        assert len(handles) == 4 and len(tt._active_environments) == 4
        for i, profile in enumerate(homes):
            token = set_hermes_home_override(profile)
            try:
                own = {handle for owner, handle in handles if owner == profile}
                listed = call("process", {"action": "list", "node": "macstudio"})
                assert {row["session_id"] for row in listed["processes"]} == own
                foreign = next(handle for owner, handle in handles if owner != profile)
                before = len(transports)
                denied = call("process", {"action": "kill", "session_id": foreign})
                assert "different profile" in denied["error"] and len(transports) == before
                assert registry.kill_all("raw-owner", source="process.kill") == 2
                if i == 0:
                    assert all(not registry._lookup_session(h).exited for owner, h in handles if owner != profile)
                    assert "macstudio" in call("read_file", {"path": str(roots["macstudio"] / "same.txt")}, session="session-0")["content"]
                else:
                    refused = call("write_file", {"path": str(roots["macstudio"] / "same.txt"), "content": "cross-profile"}, session="session-0")
                    assert "Refusing to overwrite" in refused["error"]
            finally:
                reset_hermes_home_override(token)
    finally:
        from tools.terminal_tool_lifecycle import cleanup_vm
        for profile in homes:
            token = set_hermes_home_override(profile)
            try:
                cleanup_vm("raw-owner")
                ft.clear_file_ops_cache("raw-owner")
            finally:
                reset_hermes_home_override(token)


def test_compaction_keeps_node_baselines_without_controller_stat(peers, monkeypatch):
    import tools.file_tools_read_tracking as tracking
    roots, logical, _, _, _, _ = peers
    remote = roots["macstudio"] / "DIGEST.txt"
    remote.write_text("baseline\n")
    path = logical + "/DIGEST.txt"
    assert "baseline" in call("read_file", {"path": path})["content"]
    original = os.stat

    def checked(path, *args, **kwargs):
        assert not str(path).startswith(logical)
        return original(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(os, "stat", checked)
        tracking.reset_file_dedup("raw-owner")
        tracking.notify_other_tool_call("raw-owner")
    assert call("write_file", {"path": path, "content": "after compaction\n"}).get("verified")
    assert remote.read_text() == "after compaction\n"
    # A matching mtime and length do not authorize a changed remote baseline.
    stamp = remote.stat()
    remote.write_text("different content\n")
    os.utime(remote, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    result = call("write_file", {"path": path, "content": "must-not-overwrite\n"})
    assert "Refusing to overwrite" in result["error"]
    assert remote.read_text() == "different content\n"


def test_offset_reads_and_v4a_patch_stay_on_selected_node(peers):
    roots, logical, _, _, _, _ = peers
    for node, root in roots.items():
        (root / "pages.txt").write_text(f"{node}\nline2\nline3\n")
    path = logical + "/pages.txt"
    assert "line2" in call("read_file", {"path": path, "offset": 2, "limit": 1})["content"]
    assert "Refusing to overwrite" in call("write_file", {"path": path, "content": "partial baseline"})["error"]
    call("read_file", {"path": path, "node": "macbook"})
    body = "*** Begin Patch\n*** Update File: " + path + "\n@@\n-line2\n+patched\n*** End Patch"
    result = call("patch", {"node": "macbook", "mode": "patch", "patch": body})
    assert "error" not in result, result
    assert "patched" in (roots["macbook"] / "pages.txt").read_text()
    assert "patched" not in (roots["macstudio"] / "pages.txt").read_text()


def test_acp_named_edits_refuse_before_controller_diff(peers, monkeypatch):
    from acp_adapter import edit_approval
    roots, logical, _, _, _, _ = peers
    opened = []
    monkeypatch.setattr(edit_approval, "_read_text_if_exists", lambda path: opened.append(path))
    token = edit_approval.set_edit_approval_requester(lambda proposal: True)
    try:
        for args in ({"path": logical + "/same.txt", "content": "forbidden"},
                     {"path": logical + "/same.txt", "content": "forbidden", "node": "macbook"}):
            result = call("write_file", args)
            assert "unsupported" in result["error"] and "node" in result["error"]
    finally:
        edit_approval.reset_edit_approval_requester(token)
    assert opened == []
    assert (roots["macstudio"] / "same.txt").read_text() == "macstudio\nsecond\n"


def test_offline_process_and_conflicting_handle_never_probe_other_node(peers):
    _, _, offline, transports, registry, _ = peers
    launched = call("terminal", {"node": "macbook", "command": "exec sleep 30", "background": True})
    handle = launched["session_id"]
    before = len(transports)
    refused = call("process", {"action": "kill", "session_id": handle, "node": "macstudio"})
    assert "conflicts" in refused["error"] and len(transports) == before
    # An explicit list for the Studio must not inspect a MacBook handle either.
    listed = call("process", {"action": "list", "node": "macstudio"})
    assert listed["processes"] == [] and len(transports) == before
    offline.add("macbook")
    result = call("process", {"action": "poll", "session_id": handle})
    assert result["node"] == "macbook" and result["remote_status"] == "unavailable"
    assert "unavailable" in result["remote_error"]
    assert not registry._lookup_session(handle).exited
    offline.clear()
    assert call("process", {"action": "kill", "session_id": handle})["status"] == "killed"
