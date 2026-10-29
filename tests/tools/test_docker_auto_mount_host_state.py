"""Host-state mount policy must apply before collection and across container reuse."""

from contextlib import nullcontext
import subprocess
from unittest.mock import Mock

import pytest

from tools import credential_files
from tools.environments import docker as docker_env


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    """A label-filtering Docker runner, including containers from older processes."""
    containers = {}
    commands = []
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env, "_cgroup_limits_available", lambda image: False)

    def run(cmd, **kwargs):
        commands.append(cmd)
        output = ""
        if cmd[1] == "run":
            cid = f"container-{len(containers)}"
            labels = dict(cmd[i + 1].split("=", 1) for i, v in enumerate(cmd) if v == "--label")
            containers[cid] = labels
            output = cid + "\n"
        elif cmd[1] == "ps":
            filters = [cmd[i + 1].removeprefix("label=").split("=", 1)
                       for i, v in enumerate(cmd) if v == "--filter"]
            output = "".join(f"{cid}\trunning\n" for cid, labels in containers.items()
                             if all(labels.get(k) == v for k, v in filters))
        elif cmd[1] == "inspect" and ".Config.Image" in cmd[3]:
            output = "python:3.14\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")

    monkeypatch.setattr(docker_env.subprocess, "run", run)
    return containers, commands, home


def _getters(monkeypatch, *, mounts=None):
    mocks = []
    for name, _, _ in docker_env._RO_MOUNT_SOURCES:
        getter = (Mock(return_value=mounts[name]) if mounts is not None else
                  Mock(side_effect=AssertionError(f"{name} must not run")))
        monkeypatch.setattr(credential_files, name, getter)
        mocks.append(getter)
    return mocks


def _environment(**kwargs):
    return docker_env.DockerEnvironment(image="python:3.14", task_id="mount-policy", **kwargs)


def _binds(commands):
    run = next(cmd for cmd in reversed(commands) if cmd[1] == "run")
    return [run[i + 1] for i, value in enumerate(run) if value == "-v"]


@pytest.mark.parametrize("volumes", [[], ["chosen-volume:/chosen:ro"]])
def test_disabled_skips_collectors_and_preserves_explicit_volumes(monkeypatch, daemon, volumes):
    getters = _getters(monkeypatch)
    _, commands, home = daemon
    _environment(auto_mount_host_state=False, volumes=volumes)
    assert _binds(commands) == volumes
    for getter in getters:
        getter.assert_not_called()
    assert not (home / "cache").exists()
    assert not (home / "attachments").exists()


@pytest.mark.parametrize("kwargs", [{}, {"auto_mount_host_state": True}])
def test_default_and_true_keep_all_automatic_mounts(monkeypatch, daemon, tmp_path, kwargs):
    credential = tmp_path / "token.json"
    credential.write_text("{}")
    skills = tmp_path / "skills"
    skills.mkdir()
    cache = tmp_path / "documents"
    cache.mkdir()
    entries = [(credential, "/root/.hermes/token.json"),
               (skills, "/root/.hermes/skills"), (cache, "/root/.hermes/cache/documents")]
    mounts = {name: [{"host_path": str(src), "container_path": dst}]
              for (name, _, _), (src, dst) in zip(docker_env._RO_MOUNT_SOURCES, entries, strict=True)}
    getters = _getters(monkeypatch, mounts=mounts)
    _, commands, _ = daemon
    _environment(**kwargs)
    assert _binds(commands) == [f"{src}:{dst}:ro" for src, dst in entries]
    for getter in getters:
        getter.assert_called_once_with()


@pytest.mark.parametrize("shared_key", ["", "trusted-shared"])
@pytest.mark.parametrize("old_policy,new_policy", [(None, False), (True, False), (False, True)])
def test_reuse_never_crosses_mount_policy(monkeypatch, daemon, shared_key, old_policy, new_policy):
    # Empty collectors prove the policy is identity, not just a side effect of differing mount lists.
    _getters(monkeypatch, mounts={name: [] for name, _, _ in docker_env._RO_MOUNT_SOURCES})
    containers, commands, _ = daemon
    first = _environment(auto_mount_host_state=old_policy is not False, shared_container_key=shared_key)
    if old_policy is None:
        # Legacy containers predate the policy label and could carry automatic binds.
        containers[first._container_id].pop("hermes-auto-mount-host-state", None)
    second = _environment(auto_mount_host_state=new_policy, shared_container_key=shared_key)
    assert second._container_id != first._container_id
    third = _environment(auto_mount_host_state=new_policy, shared_container_key=shared_key)
    assert third._container_id == second._container_id
    assert len([cmd for cmd in commands if cmd[1] == "run"]) == 2
    assert third._recreate_container()
    assert third._container_id == second._container_id


@pytest.mark.parametrize("legacy", [False, True])
def test_disabled_rejects_container_with_automatic_cache_bind(monkeypatch, daemon, tmp_path, legacy):
    cache = tmp_path / "cache-source"
    cache.mkdir()
    mounts = {name: [] for name, _, _ in docker_env._RO_MOUNT_SOURCES}
    mounts["get_cache_directory_mounts"] = [
        {"host_path": str(cache), "container_path": "/root/.hermes/cache/documents"}]
    _getters(monkeypatch, mounts=mounts)
    containers, commands, _ = daemon
    first = _environment(shared_container_key="trusted-shared")
    assert _binds(commands) == [f"{cache}:/root/.hermes/cache/documents:ro"]
    if legacy:
        containers[first._container_id].pop("hermes-auto-mount-host-state")

    getters = _getters(monkeypatch)
    second = _environment(auto_mount_host_state=False, shared_container_key="trusted-shared")
    assert second._container_id != first._container_id
    assert _binds(commands) == []
    for getter in getters:
        getter.assert_not_called()


def test_config_defaults_and_backend_default_are_enabled(monkeypatch):
    from hermes_cli.config import DEFAULT_CONFIG
    from tools.terminal_tool import _get_env_config
    from tools.terminal_tool_backends import _container_config_from_config

    monkeypatch.delenv("TERMINAL_DOCKER_AUTO_MOUNT_HOST_STATE", raising=False)
    assert DEFAULT_CONFIG["terminal"]["docker_auto_mount_host_state"] is True
    assert _get_env_config()["docker_auto_mount_host_state"] is True
    assert _container_config_from_config({})["docker_auto_mount_host_state"] is True


@pytest.mark.parametrize("bridge", ["cli", "gateway", "config", "profile"])
def test_config_false_reaches_docker_constructor(monkeypatch, daemon, bridge):
    from hermes_cli.config import apply_terminal_config_to_env, load_config_readonly
    from tools import terminal_tool
    from tools.terminal_tool_backends import _build_docker_env, _container_config_from_config
    from tools.terminal_scope import install_and_reset_profile_terminal_scope

    getters = _getters(monkeypatch)
    _, commands, home = daemon
    (home / "config.yaml").write_text(
        "terminal:\n  backend: docker\n  container_persistent: false\n"
        "  docker_orphan_reaper: false\n  docker_auto_mount_host_state: false\n"
        "  docker_volumes: [chosen-volume:/chosen:ro]\n")
    config = load_config_readonly()
    monkeypatch.setenv("TERMINAL_DOCKER_AUTO_MOUNT_HOST_STATE", "true")
    if bridge == "cli":
        from hermes_cli.cli_config_load import _mirror_config_to_env
        _mirror_config_to_env(config, True)
    elif bridge == "gateway":
        from gateway.run import _bridge_terminal_config_to_env
        _bridge_terminal_config_to_env(config["terminal"])
    elif bridge == "config":
        apply_terminal_config_to_env()

    scope = install_and_reset_profile_terminal_scope(home) if bridge == "profile" else nullcontext()
    with scope:
        resolved = terminal_tool._get_env_config()
        assert resolved["docker_auto_mount_host_state"] is False
        cc = _container_config_from_config(resolved)
        _build_docker_env(image="python:3.14", cwd="/root", timeout=60,
                          cc=cc, task_id="default", host_cwd=None)
    assert _binds(commands) == ["chosen-volume:/chosen:ro"]
    for getter in getters:
        getter.assert_not_called()
