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

    source = tmp_path / "source-revisions"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    revisions = {}
    for name in ("A", "B"):
        (source / "version.txt").write_text(name, encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "version.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=S2", "-c", "user.email=s2@example.test",
                        "-c", "commit.gpgsign=false", "commit", "-qm", name], check=True)
        revisions[name] = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    sha_a, sha_b = revisions["A"], revisions["B"]
    for name, sha in revisions.items():
        release = home / "releases" / sha
        release.mkdir(parents=True)
        venv.EnvBuilder(with_pip=False).create(release / ".venv")
        package = release / "hermes_cli"
        package.mkdir()
        (package / "__init__.py").write_text(f"RELEASE = '{name}'\n")
        (package / "main.py").write_text("# release identity root for fleet probe\n")
        (release / ".release-ready").write_text(sha + "\n", encoding="utf-8")
        (release / "probe.py").write_text(
            "import hermes_cli, json, os, pathlib, sys, time\n"
            "p=pathlib.Path(os.environ['S2_PROBE_OUTPUT'])\n"
            "p.write_text(json.dumps({'pid':os.getpid(),'exe':sys.executable,"
            "'cwd':os.getcwd(),'module':hermes_cli.__file__,"
            "'release':hermes_cli.RELEASE}))\n"
            "time.sleep(120)\n"
        )
    promote(home, home / "releases" / sha_a)
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
                        root = home / "releases" / (sha_a if name == "A" else sha_b)
                        for key in ("exe", "cwd", "module"):
                            assert Path(result[key]).resolve().is_relative_to(root)
                        return result
            time.sleep(.1)
        raise AssertionError(f"no launchd process for {name}; logs: {list(logs.glob('*'))}")

    target = f"{domain}/{label}"
    try:
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True, timeout=15)
        a = observed("A")
        promote(home, home / "releases" / sha_b)
        subprocess.run(["launchctl", "kickstart", "-k", target], check=True, timeout=90)
        b = observed("B", a["pid"])
        assert b["pid"] != a["pid"]
        assert (home / "current").resolve() == home / "releases" / sha_b
        # Exercise the updater's real fleet restart + verification + receipt path.
        # Discover ONLY this throwaway label; the account's ai.hermes.gateway job
        # is neither enumerated nor eligible for any restart/kill helper.
        from types import SimpleNamespace
        from hermes_cli import update_cmd, update_cmd_fleet, update_receipt
        monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: path)
        monkeypatch.setattr(gateway, "launchd_gateway_labels_for_install", lambda: [label])
        monkeypatch.setattr(gateway, "legacy_launchd_labels_for_install", lambda **kw: [])
        monkeypatch.setattr(gateway, "_launchd_domain", lambda: domain)
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kw: [])
        monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **kw: [])
        monkeypatch.setattr(gateway, "_wait_for_api_server_port_free", lambda: None)
        monkeypatch.setattr(update_cmd_fleet, "_restart_systemd_gateway_units", lambda *args: None)
        monkeypatch.setattr(update_cmd_fleet, "_restart_manual_gateways", lambda *args: None)
        monkeypatch.setattr(update_cmd_fleet, "_force_kill_stuck_gateways", lambda *args: None)
        monkeypatch.setattr(update_cmd_fleet, "_print_legacy_units_warning", lambda: None)
        monkeypatch.setattr(update_cmd, "_finish_dashboard_update_cleanup", lambda *args, **kw: None)
        monkeypatch.setattr(update_cmd, "_surviving_pre_update_serve_runtimes", lambda *args: [])
        monkeypatch.setattr(update_cmd._m(), "_fleet_probe_expected_runtimes", lambda *args: True)
        monkeypatch.setattr(update_receipt, "_profile_homes", lambda: [("default", home)])
        def real_process_socket_identity(_home):
            import psutil
            try:
                row = json.loads(output.read_text(encoding="utf-8"))
                process = psutil.Process(row["pid"])
                root = Path(row["cwd"]).resolve()
                if not process.is_running() or Path(process.cwd()).resolve() != root:
                    return None
                return row["pid"], {"code_sha": root.name}
            except (ValueError, OSError, psutil.Error):
                return None
        monkeypatch.setattr(update_receipt, "_socket_identity", real_process_socket_identity)
        monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
        # Real update/rollback verification must invoke retention, not only
        # unit-call retain(). Protect a live worker cwd and an explicit receipt.
        extra = []
        for i in range(7):
            old = home / "releases" / f"old-{i}"
            old.mkdir()
            (old / ".release-ready").write_text(old.name + "\n")
            os.utime(old, (i + 1, i + 1))
            extra.append(old)
        pins_dir = home / "logs" / "update_receipts"
        pins_dir.mkdir(parents=True, exist_ok=True)
        (pins_dir / "worker-pin.json").write_text(json.dumps({"release_path": str(extra[1])}))
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], cwd=extra[0])
        import psutil
        try:
            assert Path(psutil.Process(worker.pid).cwd()).resolve() == extra[0]
            update_cmd._cmd_update_impl(SimpleNamespace(rollback=True), gateway_mode=False)
            assert extra[0].exists() and extra[1].exists()
            # A process with unreadable psutil attributes conservatively pins
            # all releases on this host. Exact pruning is covered by the
            # deterministic retention policy test; real PID safety is here.
        finally:
            worker.terminate()
            worker.wait(timeout=5)
        rolled_back = observed("A", b["pid"])
        assert rolled_back["pid"] != b["pid"]
        assert (home / "current").resolve() == home / "releases" / sha_a
        assert (home / "previous").resolve() == home / "releases" / sha_b
        receipt = json.loads((home / "logs/update_receipts/latest.json").read_text(encoding="utf-8"))
        assert receipt["outcome"] == "success"
        assert receipt["gateway_restart"]["restarted_services"] == [label]
        assert len(receipt["fleet"]) == 1 and receipt["fleet"][0]["state"] == "current"
        assert receipt["fleet"][0]["code_sha"] == sha_a
        assert receipt["release_transition"] == {
            "from_sha": sha_b, "to_sha": sha_a,
            "from_path": str(home / "releases" / sha_b),
            "to_path": str(home / "releases" / sha_a), "kind": "rollback",
        }
        assert any(s["name"] == "immutable_rollback" and f"from_sha={sha_b}" in s["detail"]
                   and f"to_sha={sha_a}" in s["detail"] for s in receipt["steps"])
    finally:
        subprocess.run(["launchctl", "bootout", target], capture_output=True, timeout=15)
        assert subprocess.run(["launchctl", "print", target], capture_output=True).returncode != 0


@pytest.mark.skipif(sys.platform != "darwin", reason="requires launchd")
def test_first_migration_and_source_plist_reversal_real_process(tmp_path, monkeypatch):
    """Updater promotion and reversal reload one throwaway job, never the live label."""
    from hermes_cli import immutable_releases as releases, update_cmd, update_receipt

    home = tmp_path / "profile"
    home.mkdir()
    label = f"ai.hermes.s2migration.{uuid.uuid4().hex}"
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{label}"
    source = tmp_path / "source"
    source.mkdir()
    (source / "hermes_cli").mkdir()
    (source / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (source / "probe.py").write_text(
        "import json, os, pathlib, sys, time\n"
        "pathlib.Path(os.environ['S2_PROBE_OUTPUT']).write_text(json.dumps("
        "{'pid': os.getpid(), 'cwd': os.getcwd(), 'exe': sys.executable, "
        "'release': pathlib.Path(__file__).resolve().parent.name}))\n"
        "time.sleep(120)\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "probe.py"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=S2", "-c", "user.email=s2@example.test",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "source"], check=True)
    sha = releases.release_sha(source)
    b = home / "releases" / sha
    b.mkdir(parents=True)
    venv.EnvBuilder(with_pip=False).create(b / ".venv")
    (b / "probe.py").write_text((source / "probe.py").read_text(encoding="utf-8"), encoding="utf-8")
    (b / ".release-ready").write_text(sha + "\n", encoding="utf-8")
    output = home / "observed.json"
    plist_path = tmp_path / f"{label}.plist"

    def definition(root, python):
        return plistlib.dumps({
            "Label": label, "RunAtLoad": True, "KeepAlive": False,
            "WorkingDirectory": str(root),
            "ProgramArguments": [str(python), str(root / "probe.py")],
            "EnvironmentVariables": {"HERMES_HOME": str(home), "S2_PROBE_OUTPUT": str(output)},
        })

    original = definition(source, sys.executable)
    replacement = definition(home / "current", home / "current" / ".venv/bin/python")
    plist_path.write_bytes(original)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: label)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(gateway, "generate_launchd_plist", lambda: replacement.decode("utf-8"))
    monkeypatch.setattr(gateway, "launchd_plist_is_current", lambda: plist_path.read_bytes() == replacement)
    monkeypatch.setattr(gateway, "_launchd_domain", lambda: domain)
    # Only the throwaway plist is in scope; preserve production's temp-home guard.
    monkeypatch.setattr(gateway, "_refuse_temp_home_service_write", lambda *_: False)
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"immutable_releases": True})
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", source)
    monkeypatch.setattr(releases, "stage_release", lambda *args, **kwargs: (b, "staged"))

    def observed(name, old_pid=None):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if output.exists():
                try:
                    row = json.loads(output.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    pass
                else:
                    if row["release"] == name and row["pid"] != old_pid:
                        return row
            time.sleep(.1)
        raise AssertionError(f"{name} did not start through launchd")

    try:
        subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True, timeout=15)
        a = observed("source")
        # Inject failure after the candidate plist is written, before launchd
        # adopts it. Both durable bytes and pointers must return to old state.
        real_refresh = gateway.refresh_launchd_plist_if_needed
        def failed_refresh():
            plist_path.write_bytes(replacement)
            return False
        monkeypatch.setattr(gateway, "refresh_launchd_plist_if_needed", failed_refresh)
        update_receipt.begin_update_receipt()
        assert update_cmd._activate_immutable_release() is False
        failed_receipt = update_receipt.finalize_update_receipt("partial")
        assert failed_receipt is not None
        assert plist_path.read_bytes() == original
        assert not (home / "current").exists()
        assert not (home / "previous").exists()
        recovered = observed("source", a["pid"])
        assert Path(recovered["cwd"]).resolve() == source
        monkeypatch.setattr(gateway, "refresh_launchd_plist_if_needed", real_refresh)
        update_receipt.begin_update_receipt()
        assert update_cmd._activate_immutable_release()
        updated_receipt = update_receipt.finalize_update_receipt("success")
        assert updated_receipt is not None
        recorded = json.loads(updated_receipt.read_text(encoding="utf-8"))
        assert any(s["name"] == "immutable_release" and "migration=True" in s["detail"]
                   and sha in s["detail"] for s in recorded["steps"])
        assert recorded["release_transition"] == {
            "from_sha": sha, "to_sha": sha, "from_path": str(source),
            "to_path": str(b), "kind": "migration",
        }
        assert (home / "current").resolve() == b
        assert (home / "previous").resolve() == source
        assert plist_path.read_bytes() == replacement
        promoted = observed(sha, a["pid"])
        assert Path(promoted["cwd"]).resolve() == b
        assert Path(promoted["exe"]).resolve().is_relative_to(b)
        from types import SimpleNamespace
        monkeypatch.setattr(update_cmd, "_resolve_update_options", lambda *args: SimpleNamespace())
        def restart_throwaway(*args):
            subprocess.run(["launchctl", "kickstart", "-k", target], check=True, timeout=90)
            return SimpleNamespace(incomplete=False)
        monkeypatch.setattr(update_cmd, "_restart_gateway_fleet_after_update", restart_throwaway)
        def verify_throwaway(restart, **kwargs):
            row = observed("source", promoted["pid"])
            assert Path(row["cwd"]).resolve() == source
            update_receipt.finalize_update_receipt("success", fleet=[{
                "pid": row["pid"], "observed_root": row["cwd"], "state": "probe",
            }])
        monkeypatch.setattr(update_cmd, "_verify_fleet_after_update", verify_throwaway)
        update_cmd._cmd_update_impl(SimpleNamespace(rollback=True), gateway_mode=False)
        assert (home / "current").resolve() == source
        original_plist = releases.migration_plist(home)
        assert original_plist is not None
        saved_path, saved_body = original_plist
        assert saved_path == plist_path and saved_body == original
        restored = observed("source", promoted["pid"])
        assert restored["pid"] != a["pid"]
        assert Path(restored["cwd"]).resolve() == source
        assert plist_path.read_bytes() == original
        rollback_receipt = json.loads((home / "logs/update_receipts/latest.json").read_text(encoding="utf-8"))
        assert rollback_receipt["outcome"] == "success"
        assert rollback_receipt["release_transition"] == {
            "from_sha": sha, "to_sha": sha, "from_path": str(b),
            "to_path": str(source), "kind": "migration_reversal",
        }
        assert any(s["name"] == "immutable_rollback" and s["ok"] for s in rollback_receipt["steps"])
    finally:
        subprocess.run(["launchctl", "bootout", target], capture_output=True, timeout=15)
        assert subprocess.run(["launchctl", "print", target], capture_output=True).returncode != 0
