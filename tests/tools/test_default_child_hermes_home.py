"""Default-profile terminal and execute_code children do not receive the live home.

A routed named profile and a cron worker still receive the home that identifies their profile.
The persistent shell snapshot follows the same rule so later commands cannot replay the value.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import hermes_constants
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.code_execution_env import _build_child_env
from tools.environments.local import LocalEnvironment, _make_run_env
from tools.process_registry import ProcessRegistry

_ROOT = Path(__file__).resolve().parents[2]


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


def _child_resolved_home(env: dict[str, str]) -> Path:
    child_env = dict(os.environ)
    child_env.pop("HERMES_HOME", None)
    child_env.update(env)
    child_env["PYTHONPATH"] = str(_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", "from hermes_constants import get_hermes_home; print(get_hermes_home())"],
        env=child_env, capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip())


class _SnapshotEnv(LocalEnvironment):
    def cleanup(self) -> None:
        return None


def test_default_profile_terminal_children_do_not_see_live_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert "HERMES_HOME" not in _make_run_env({})
    assert "HERMES_HOME" not in ProcessRegistry._spawn_env({})
    assert "HERMES_HOME" not in _child_env()


def test_plain_default_child_resolves_its_default_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))

    child_env = _make_run_env({})
    assert "HERMES_HOME" not in child_env
    assert _child_resolved_home(child_env) == home


def test_profile_home_child_keeps_and_resolves_explicit_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    (home / "home").mkdir(parents=True)
    real_home = tmp_path / "user"
    real_home.mkdir()
    monkeypatch.setattr(hermes_constants, "is_container", lambda: False)
    monkeypatch.setenv("TERMINAL_HOME_MODE", "profile")
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_HOME", str(home))

    child_env = _make_run_env({})
    assert child_env["HERMES_HOME"] == str(home)
    assert child_env["HOME"] == str(home / "home")
    assert _child_resolved_home(child_env) == home


def test_container_auto_profile_home_child_keeps_and_resolves_explicit_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    (home / "home").mkdir(parents=True)
    real_home = tmp_path / "user"
    real_home.mkdir()
    monkeypatch.setattr(hermes_constants, "is_container", lambda: True)
    monkeypatch.delenv("TERMINAL_HOME_MODE", raising=False)
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_HOME", str(home))

    child_env = _make_run_env({})
    assert child_env["HERMES_HOME"] == str(home)
    assert child_env["HOME"] == str(home / "home")
    assert _child_resolved_home(child_env) == home


def test_delegated_child_explicit_kanban_home_overrides_lineage_fallback(
    tmp_path, monkeypatch
):
    from agent.delegation_context import (
        DELEGATED_CHILD_ENV_MARKER,
        KANBAN_HOME_INJECTED_ENV,
        scrub_kanban_env,
    )
    from hermes_cli import kanban_db as kb

    lineage_root = tmp_path / "lineage"
    explicit_root = tmp_path / "explicit"
    lineage_root.mkdir()
    explicit_root.mkdir()
    child_env = scrub_kanban_env({
        "HERMES_HOME": str(lineage_root),
        "HERMES_KANBAN_HOME": str(explicit_root),
    })
    monkeypatch.setenv("HERMES_HOME", child_env["HERMES_HOME"])
    monkeypatch.setenv("HERMES_KANBAN_HOME", child_env["HERMES_KANBAN_HOME"])
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, child_env[DELEGATED_CHILD_ENV_MARKER])

    assert kb.kanban_home() == explicit_root
    assert child_env[DELEGATED_CHILD_ENV_MARKER] == child_env["HERMES_KANBAN_HOME"]
    assert KANBAN_HOME_INJECTED_ENV not in child_env


def test_delegated_child_marks_injected_kanban_home_fallback(tmp_path, monkeypatch):
    from agent.delegation_context import (
        DELEGATED_CHILD_ENV_MARKER,
        KANBAN_HOME_INJECTED_ENV,
        delegated_child_context,
        is_injected_kanban_home,
        scrub_kanban_env,
    )

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv(KANBAN_HOME_INJECTED_ENV, raising=False)

    with delegated_child_context():
        child_env = scrub_kanban_env(dict(os.environ))

    assert child_env[KANBAN_HOME_INJECTED_ENV] == "1"
    assert child_env[DELEGATED_CHILD_ENV_MARKER] == child_env["HERMES_KANBAN_HOME"]
    monkeypatch.setenv("HERMES_KANBAN_HOME", child_env["HERMES_KANBAN_HOME"])
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, child_env[DELEGATED_CHILD_ENV_MARKER])
    monkeypatch.setenv(KANBAN_HOME_INJECTED_ENV, child_env[KANBAN_HOME_INJECTED_ENV])
    assert is_injected_kanban_home()


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
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))

    env = _cron_worker_env(monkeypatch, home)
    assert env["HERMES_HOME"] == str(home)


def test_default_profile_snapshot_does_not_replay_live_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
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
    monkeypatch.setenv("HOME", str(tmp_path))
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
