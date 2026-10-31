"""Disposable launchd cleanup stays scoped even when pytest removes a session tree."""

import os
import subprocess
from pathlib import Path

import pytest

from tests.hermes_cli.immutable_launchd_cleanup import _sweep_missing_plists


@pytest.mark.platforms("macos")
def test_missing_pytest_plist_reaps_only_disposable_job(tmp_path, monkeypatch):
    # Absolute paths as launchctl reports them. The non-pytest path cannot sit under tmp_path,
    # which is itself a pytest session tree.
    runner = Path("/private/var/tmp/hermes-pytest-501/r-gone/pytest-of-user/pytest-0/case")
    default = Path("/private/var/folders/xy/abc/T/pytest-of-user/pytest-1046/case0")
    live = tmp_path / "pytest-of-user" / "pytest-1" / "case" / "ai.hermes.s2crash.active.plist"
    live.parent.mkdir(parents=True)
    live.touch()
    labels = ("ai.hermes.s2spike.orphan", "ai.hermes.s2crash.default", "ai.hermes.s2crash.active",
              "ai.hermes.s2migration.other", "ai.hermes.gateway")
    paths = dict(zip(labels, (runner / "ai.hermes.s2spike.orphan.plist",
                              default / "ai.hermes.s2crash.default.plist", live,
                              Path("/private/var/tmp/other/ai.hermes.s2migration.other.plist"),
                              Path("/private/var/tmp/pytest-of-user/pytest-2/ai.hermes.gateway.plist"))))
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
    _sweep_missing_plists()
    assert [command for command in commands if command[:2] == ["launchctl", "bootout"]] == [
        ["launchctl", "bootout", f"gui/{os.getuid()}/ai.hermes.s2spike.orphan"],
        ["launchctl", "bootout", f"gui/{os.getuid()}/ai.hermes.s2crash.default"],
    ]
    assert not any("ai.hermes.gateway" in " ".join(command) for command in commands[1:])
