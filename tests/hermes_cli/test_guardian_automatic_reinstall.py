"""Existing automatic guardian definitions remain reinstallable without replacement."""
import os
import plistlib
from types import SimpleNamespace

import pytest

from hermes_cli import gateway, gateway_guardian as guardian


@pytest.mark.platforms("macos")
def test_automatic_guardian_reinstall_is_an_idempotent_noop(tmp_path, monkeypatch, capsys):
    import pwd
    home = tmp_path / "hermes"
    release = home / "releases" / ("a" * 40)
    (release / ".venv/bin").mkdir(parents=True)
    (release / ".venv/bin/python").touch()
    (home / "current").symlink_to(release)
    path = tmp_path / "gateway.plist"
    label = "ai.hermes.gateway"
    path.write_bytes(plistlib.dumps({"Label": label, "EnvironmentVariables": {"HERMES_HOME": str(home)}}))
    domain = f"gui/{os.getuid()}"
    # The pre-lifetime renderer had no session eligibility key, even for GUI jobs.
    original = plistlib.dumps({
        "Label": guardian.GUARDIAN_LABEL, "RunAtLoad": True, "StartInterval": guardian.INTERVAL,
        "ProgramArguments": [str(home / "current/.venv/bin/python"), "-m", "hermes_cli.gateway_guardian", "run",
                             "--gateway-plist", str(path), "--gateway-label", label, "--domain", domain],
        "WorkingDirectory": str(home / "current"), "EnvironmentVariables": {"HERMES_HOME": str(home)},
        "StandardOutPath": str(home / "logs/guardian/stdout.log"),
        "StandardErrorPath": str(home / "logs/guardian/stderr.log"),
    })
    installed = tmp_path / "Library/LaunchAgents" / f"{guardian.GUARDIAN_LABEL}.plist"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(original)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    monkeypatch.setattr(guardian, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: path)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: label)
    monkeypatch.setattr(guardian, "_domain", lambda name: domain)
    monkeypatch.setattr(guardian, "enabled", lambda home: True)
    monkeypatch.setattr(guardian, "_release_is_ready", lambda *a: True)
    monkeypatch.setattr(guardian, "_launch_state", lambda *a: "loaded")
    monkeypatch.setattr(guardian.subprocess, "run", lambda *a, **kw: pytest.fail("reinstall restarted guardian"))
    assert guardian.cli(["install"]) == 0
    assert guardian.cli(["install"]) == 0
    assert installed.read_bytes() == original
    assert capsys.readouterr().out.count("Guardian already installed") == 2
