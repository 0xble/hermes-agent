"""Disposable macOS launchd proof; never uses the production label or profile."""
import json
import os
import plistlib
import subprocess
import sys
import time
import uuid
import venv
from pathlib import Path

import pytest

from hermes_cli import gateway, gateway_launchd
from hermes_cli.immutable_releases import promote


@pytest.mark.skipif(sys.platform != "darwin", reason="requires launchd")
def test_launchd_resolves_current_on_each_spawn(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    label = f"ai.hermes.s2spike.{uuid.uuid4().hex}"
    domain = f"gui/{os.getuid()}"
    output = home / "observed.json"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: label)
    monkeypatch.setattr(gateway_launchd, "get_launchd_label", lambda: label)

    for name in ("A", "B"):
        release = home / "releases" / name
        release.mkdir(parents=True)
        venv.EnvBuilder(with_pip=False).create(release / ".venv")
        package = release / "hermes_cli"
        package.mkdir()
        (package / "__init__.py").write_text(f"RELEASE = '{name}'\n")
        (release / ".release-ready").write_text(name + "\n", encoding="utf-8")
        (release / "probe.py").write_text(
            "import hermes_cli, json, os, pathlib, sys, time\n"
            "p=pathlib.Path(os.environ['S2_PROBE_OUTPUT'])\n"
            "p.write_text(json.dumps({'pid':os.getpid(),'exe':sys.executable,"
            "'cwd':os.getcwd(),'module':hermes_cli.__file__,"
            "'release':hermes_cli.RELEASE}))\n"
            "time.sleep(120)\n"
        )
    promote(home, home / "releases" / "A")
    plist = plistlib.loads(gateway.generate_launchd_plist().encode())
    assert plist["Label"] == label
    assert plist["WorkingDirectory"] == str(home / "current")
    assert str(home / "current" / ".venv" / "bin" / "python") in " ".join(plist["ProgramArguments"])
    assert plist["EnvironmentVariables"]["VIRTUAL_ENV"] == str(home / "current" / ".venv")
    # Preserve generated interpreter/cwd/environment and launchd wrapper; replace
    # only the gateway payload with a bounded, no-credential process probe.
    python = str(home / "current" / ".venv" / "bin" / "python")
    logs = home / "logs"
    plist["ProgramArguments"] = gateway_launchd.launchd_program_arguments(
        [python, "-m", "probe"], logs / "stdout.log", logs / "stderr.log"
    )
    plist["EnvironmentVariables"]["S2_PROBE_OUTPUT"] = str(output)
    plist["KeepAlive"] = False
    plist["RunAtLoad"] = True
    plist.pop("LimitLoadToSessionType", None)
    path = tmp_path / f"{label}.plist"
    path.write_bytes(plistlib.dumps(plist))

    def observed(name, old_pid=None):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if output.exists():
                try:
                    result = json.loads(output.read_text())
                except (ValueError, OSError):
                    pass
                else:
                    if result["release"] == name and result["pid"] != old_pid:
                        root = home / "releases" / name
                        for key in ("exe", "cwd", "module"):
                            assert Path(result[key]).resolve().is_relative_to(root)
                        return result
            time.sleep(.1)
        raise AssertionError(f"no launchd process for {name}; logs: {list(logs.glob('*'))}")

    target = f"{domain}/{label}"
    try:
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True, timeout=15)
        a = observed("A")
        promote(home, home / "releases" / "B")
        subprocess.run(["launchctl", "kickstart", "-k", target], check=True, timeout=90)
        b = observed("B", a["pid"])
        assert b["pid"] != a["pid"]
        assert (home / "current").resolve() == home / "releases" / "B"
    finally:
        subprocess.run(["launchctl", "bootout", target], capture_output=True, timeout=15)
        assert subprocess.run(["launchctl", "print", target], capture_output=True).returncode != 0
