"""Default-profile terminal and execute_code children do not receive the live home.

A routed named profile and a cron worker still receive the home that identifies their profile.
The persistent shell snapshot follows the same rule so later commands cannot replay the value.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.code_execution_env import _build_child_env
from tools.environments.local import LocalEnvironment, _make_run_env
from tools.process_registry import ProcessRegistry


def _child_env():
    return _build_child_env(
        rpc_endpoint="sock", rpc_token="tok", tmpdir="/tmp/hermes-test", child_python=sys.executable,
    )


def _cron_worker_env(monkeypatch, home: Path) -> dict[str, str]:
    from cron import scheduler
    from tools.environments.local import build_subprocess_env, strip_launch_profile_env

    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: home)
    return strip_launch_profile_env(build_subprocess_env(
        scrub_secrets=False, inherit_profile_home=True, extra={"HERMES_HOME": str(home)},
    ))


class _SnapshotEnv(LocalEnvironment):
    def cleanup(self) -> None:
        return None


def test_default_profile_terminal_children_do_not_see_live_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert "HERMES_HOME" not in _make_run_env({})
    assert "HERMES_HOME" not in ProcessRegistry._spawn_env({})
    assert "HERMES_HOME" not in _child_env()


def test_named_profile_children_keep_their_home(monkeypatch, tmp_path):
    home = tmp_path / "profiles" / "work"
    token = set_hermes_home_override(home)
    try:
        for env in (_make_run_env({}), ProcessRegistry._spawn_env({}), _child_env()):
            assert env["HERMES_HOME"] == str(home)
    finally:
        reset_hermes_home_override(token)


def test_cron_worker_keeps_its_profile_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    env = _cron_worker_env(monkeypatch, home)
    assert env["HERMES_HOME"] == str(home)


def test_default_profile_snapshot_does_not_replay_live_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    env = _SnapshotEnv(cwd=str(tmp_path), timeout=1)

    assert "HERMES_HOME" in env._snapshot_excluded_passthrough_names()


def test_named_profile_snapshot_keeps_home(monkeypatch, tmp_path):
    home = tmp_path / "profiles" / "work"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    token = set_hermes_home_override(home)
    try:
        env = _SnapshotEnv(cwd=str(tmp_path), timeout=1, env={"HERMES_HOME": str(home)})
        assert "HERMES_HOME" not in env._snapshot_excluded_passthrough_names()
    finally:
        reset_hermes_home_override(token)


def test_pty_path_uses_the_same_spawn_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    registry = ProcessRegistry()
    captured = {}

    class _Pty:
        @staticmethod
        def spawn(argv, cwd, env, dimensions):
            captured["env"] = env
            raise RuntimeError("stop before a real PTY")

    monkeypatch.setattr(registry, "_scope_argv", lambda *args: ["bash", "-c", "true"])
    monkeypatch.setitem(sys.modules, "ptyprocess", type("M", (), {"PtyProcess": _Pty}))

    with pytest.raises(RuntimeError, match="stop before a real PTY"):
        registry._spawn_local_pty(type("S", (), {"id": "s", "cwd": str(tmp_path), "systemd_unit": ""})(), "true", {})

    assert "HERMES_HOME" not in captured["env"]
    assert captured["env"]["PYTHONUNBUFFERED"] == "1"
