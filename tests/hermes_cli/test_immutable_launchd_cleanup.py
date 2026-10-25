"""Disposable launchd cleanup stays scoped even when pytest removes a session tree."""

import os
import subprocess
from pathlib import Path

import pytest

from tests.hermes_cli.immutable_launchd_cleanup import _sweep_missing_plists


@pytest.mark.platforms("macos")
def test_missing_pytest_plist_reaps_only_disposable_job(tmp_path, monkeypatch):
    base = tmp_path / "hermes-pytest"
    session = base / "r-gone" / "pytest-of-user" / "pytest-0" / "case"
    missing = session / "ai.hermes.s2spike.orphan.plist"
    live = session / "ai.hermes.s2crash.active.plist"
    live.parent.mkdir(parents=True)
    live.touch()
    outside = tmp_path / "other" / "ai.hermes.s2migration.other.plist"
    labels = ("ai.hermes.s2spike.orphan", "ai.hermes.s2crash.active",
              "ai.hermes.s2migration.other", "ai.hermes.gateway")
    paths = dict(zip(labels, (missing, live, outside, tmp_path / "ai.hermes.gateway.plist")))
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command == ["launchctl", "list"]:
            return subprocess.CompletedProcess(command, 0,
                stdout="".join(f"-\t0\t{label}\n" for label in labels))
        if command[:2] == ["launchctl", "print"]:
            label = command[2].rsplit("/", 1)[-1]
            return subprocess.CompletedProcess(command, 0,
                stdout=f"gui/{os.getuid()}/{label} = {{\n\tpath = {paths[label]}\n}}")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    _sweep_missing_plists(base)
    assert [command for command in commands if command[:2] == ["launchctl", "bootout"]] == [
        ["launchctl", "bootout", f"gui/{os.getuid()}/ai.hermes.s2spike.orphan"]
    ]
    assert not any("ai.hermes.gateway" in " ".join(command) for command in commands[1:])
