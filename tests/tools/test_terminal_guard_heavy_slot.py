"""The terminal lifecycle guard sees through the installed heavy-slot helper on the local backend."""

import json
from pathlib import Path

import pytest

from tools.terminal_tool_guards import gateway_lifecycle_block

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def supervised(monkeypatch):
    from tools import process_registry

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)


@pytest.fixture
def trusted_slot(tmp_path, monkeypatch):
    home = tmp_path / "home"
    installed = home / ".local" / "bin" / "heavy-slot"
    installed.parent.mkdir(parents=True)
    # Same shape as the cron fixture: Python source whose /dev/null mention reads, as shell, like
    # an executed non-regular file and fails the guard closed.
    installed.write_text('#!/usr/bin/env python3\n"""/dev/null"""\n')
    installed.chmod(0o755)
    monkeypatch.setattr(Path, "home", lambda: home)
    return installed


def _block(tmp_path, command, env_type, env=None):
    return gateway_lifecycle_block(
        command=command, env=env, env_type=env_type, cwd=str(tmp_path), workdir=None,
        session_key="heavy-slot-guard-test",
    )


def test_local_heavy_slot_wrapped_test_run_is_allowed(tmp_path, supervised, trusted_slot):
    assert _block(tmp_path, f"{trusted_slot} --label proof -- true", "local") is None


def test_local_heavy_slot_still_blocks_wrapped_gateway_restart(tmp_path, supervised, trusted_slot):
    restart = tmp_path / "restart.sh"
    restart.write_text("#!/bin/sh\nlaunchctl kickstart -k gui/501/ai.hermes.gateway\n")
    restart.chmod(0o755)
    blocked = _block(tmp_path, f"{trusted_slot} --label proof -- bash {restart}", "local")
    assert blocked is not None
    assert json.loads(blocked)["exit_code"] == 1


class _RemoteEnv:
    cwd = "/"

    def execute(self, command):
        return {"returncode": 1, "output": ""}


def test_remote_backend_keeps_strict_walk(tmp_path, supervised, trusted_slot):
    """A remote callback cannot attest the local helper's identity, so the peel stays off."""
    blocked = _block(tmp_path, f"{trusted_slot} --label proof -- true", "ssh", env=_RemoteEnv())
    assert blocked is not None
    assert "/dev/null" in json.loads(blocked)["error"]
