"""The terminal boundary must scan the installed heavy-slot helper's target (#660)."""

import json
import shlex
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools import process_registry, terminal_tool
from tools.environments.local import LocalEnvironment


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("background", [False, True])
def test_installed_heavy_slot_cannot_hide_gateway_restart(tmp_path, monkeypatch, background):
    home = tmp_path / "home"
    helper = home / ".local" / "bin" / "heavy-slot"
    helper.parent.mkdir(parents=True)
    # If the local backend loses the installed-helper peel, this source refuses
    # scanning instead of reaching the wrapped script and its lifecycle command.
    helper.write_text('#!/usr/bin/env python3\n"""/dev/stdin"""\n', encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)

    script = tmp_path / "restart.sh"
    script.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
    script.chmod(0o755)
    command = f"{shlex.quote(str(helper))} --label x -- ./restart.sh"
    env = object.__new__(LocalEnvironment)
    env.cwd = str(tmp_path)
    env.env = {}
    execute = Mock(side_effect=AssertionError("The lifecycle probe must never execute a command"))
    monkeypatch.setattr(env, "execute", execute)
    monkeypatch.setattr(terminal_tool, "_acquire_env", lambda *_args: env)
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {
        "env_type": "local", "cwd": str(tmp_path), "timeout": 30, "host_cwd": None,
    })
    spawn = Mock(side_effect=AssertionError("The lifecycle probe must never spawn a command"))
    monkeypatch.setattr(terminal_tool, "spawn_background_process", spawn)

    # A benign target proves the helper itself is not the reason for rejection.
    assert terminal_tool.gateway_lifecycle_block(
        command=command, env=env, env_type="local", cwd=str(tmp_path), workdir=None,
        session_key="installed-heavy-slot-regression",
    ) is None
    script.write_text("#!/bin/sh\nhermes gateway restart\n", encoding="utf-8")
    result = json.loads(terminal_tool.terminal_tool(
        command, background=background, force=True, task_id="installed-heavy-slot-regression",
    ))

    assert result["exit_code"] == 1
    assert "cannot restart, stop, or uninstall the gateway" in result["error"]
    execute.assert_not_called()
    spawn.assert_not_called()
