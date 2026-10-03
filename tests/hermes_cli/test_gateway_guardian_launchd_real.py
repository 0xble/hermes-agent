"""A disposable real launchd guardian, never the production gateway or guardian labels."""
import json
import os
import plistlib
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from hermes_cli import gateway_guardian as guardian
from tests.hermes_cli.immutable_launchd_cleanup import register_disposable_label, sweep_prior_sessions


@pytest.fixture(scope="module", autouse=True)
def _sweep(request):
    sweep_prior_sessions(request)


@pytest.mark.platforms("macos")
def test_independent_guardian_bootstraps_unloaded_service_and_honors_stop(tmp_path, request, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    sha = "a" * 40
    release = home / "releases" / sha
    release.mkdir(parents=True)
    for name in (".release-ready", ".hermes_build_sha"):
        (release / name).write_text(sha + "\n", encoding="utf-8")
    (home / "current").symlink_to(release)
    (home / "previous").symlink_to(release)
    (home / "config.yaml").write_text("gateway:\n  guardian:\n    enabled: true\n", encoding="utf-8")
    script = tmp_path / "fake_gateway.py"
    script.write_text(
        "import json,os,pathlib,time\n"
        "from datetime import datetime,timezone\n"
        "h=pathlib.Path(os.environ['HERMES_HOME'])\n"
        "(h/'gateway_state.json').write_text(json.dumps({'pid':os.getpid(),"
        "'gateway_state':'running','code_sha':pathlib.Path.cwd().name,"
        "'updated_at':datetime.now(timezone.utc).isoformat()}))\n"
        "time.sleep(120)\n", encoding="utf-8")
    domain = f"gui/{os.getuid()}"
    suffix = uuid.uuid4().hex
    label = f"ai.hermes.s2spike.g1fake.{suffix}"
    guardian_label = f"ai.hermes.s2spike.g1guardian.{suffix}"
    gw_plist = tmp_path / f"{label}.plist"
    gw_plist.write_bytes(plistlib.dumps({"Label": label, "RunAtLoad": True, "KeepAlive": False,
        "ProgramArguments": [sys.executable, str(script)], "WorkingDirectory": str(home / "current"),
        "EnvironmentVariables": {"HERMES_HOME": str(home)}}))
    monkeypatch.setattr(guardian, "GUARDIAN_LABEL", guardian_label)
    agent_plist = tmp_path / f"{guardian_label}.plist"
    repo = Path(__file__).resolve().parents[2]
    agent_plist.write_bytes(plistlib.dumps({"Label": guardian_label, "RunAtLoad": True,
        "StartInterval": 2,
        "ProgramArguments": [sys.executable, "-m", "hermes_cli.gateway_guardian", "run",
            "--gateway-plist", str(gw_plist), "--gateway-label", label, "--domain", domain],
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {"HERMES_HOME": str(home), "PYTHONPATH": str(repo)},
        "StandardOutPath": str(tmp_path / "guardian-out.log"),
        "StandardErrorPath": str(tmp_path / "guardian-err.log")}))
    register_disposable_label(request, label, gw_plist)
    register_disposable_label(request, guardian_label, agent_plist)
    gw_target = f"{domain}/{label}"
    agent_target = f"{domain}/{guardian_label}"
    def wait_for(predicate):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(.2)
        return False
    def loaded():
        return subprocess.run(["launchctl", "print", gw_target], capture_output=True, timeout=5).returncode == 0
    try:
        subprocess.run(["launchctl", "bootstrap", domain, str(gw_plist)], check=True, timeout=15)
        assert wait_for(lambda: (home / "gateway_state.json").exists())
        subprocess.run(["launchctl", "bootstrap", domain, str(agent_plist)], check=True, timeout=15)
        assert loaded()
        subprocess.run(["launchctl", "bootout", gw_target], check=True, timeout=15)
        assert not loaded()
        start = time.monotonic()
        assert wait_for(loaded)
        assert time.monotonic() - start < 60
        assert wait_for(lambda: any(json.loads(p.read_text(encoding="utf-8")).get("outcome") == "repaired"
                   for p in (home / "logs/guardian").glob("*.json")))
        guardian.set_intent(home, stopped=True)
        subprocess.run(["launchctl", "bootout", gw_target], check=True, timeout=15)
        assert wait_for(lambda: "stopped" in (tmp_path / "guardian-out.log").read_text(encoding="utf-8"))
        assert not loaded()
        # Reproduce a completed forward switch whose new process never acknowledges.
        guardian.set_intent(home, stopped=False)
        new = home / "releases" / ("b" * 40)
        new.mkdir()
        for name in (".release-ready", ".hermes_build_sha"):
            (new / name).write_text(new.name + "\n", encoding="utf-8")
        (home / "current").unlink()
        (home / "current").symlink_to(new)
        (home / "previous").unlink()
        (home / "previous").symlink_to(release)
        definition = plistlib.loads(gw_plist.read_bytes())
        definition["WorkingDirectory"] = str(new)
        gw_plist.write_bytes(plistlib.dumps(definition))
        (home / "release-txn.json").write_text(json.dumps({
            "version": 1, "operation": "promote", "candidate": str(new),
            "previous_intended": str(release), "current_original": str(release),
            "previous_original": str(new), "requires_reload": True,
            "reload_issued": {"at": "2020-01-01T00:00:00+00:00"}}))
        subprocess.run(["launchctl", "bootstrap", domain, str(gw_plist)], check=True, timeout=15)
        assert wait_for(lambda: json.loads((home / "gateway_state.json").read_text()).get("code_sha") == new.name)
        subprocess.run(["launchctl", "bootout", gw_target], check=True, timeout=15)
        assert wait_for(lambda: (home / "current").resolve() == release)
        assert any(home.glob("release-abandoned-*.json"))
        assert wait_for(loaded)
        assert wait_for(lambda: json.loads((home / "gateway_state.json").read_text()).get("code_sha") == release.name)
        assert wait_for(lambda: any(json.loads(p.read_text(encoding="utf-8")).get("outcome") == "rolled_back"
                   for p in (home / "logs/guardian").glob("*.json")))
    finally:
        for target in (gw_target, agent_target):
            subprocess.run(["launchctl", "bootout", target], capture_output=True, timeout=15)
            assert subprocess.run(["launchctl", "print", target], capture_output=True, timeout=5).returncode != 0
        output = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
        assert label not in output and guardian_label not in output
