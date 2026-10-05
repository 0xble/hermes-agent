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
from tests.hermes_cli.immutable_launchd_cleanup import register_disposable_label, sweep_prior_sessions, install_probe_process_dependency


@pytest.fixture(scope="module", autouse=True)
def _sweep_disposable_jobs(request):
    sweep_prior_sessions(request)


def _ack_observed_probe(releases, home, plist_path, output, *, gateway_pid=None):
    """Test-only stand-in for a gateway startup ack, bound to the real throwaway job."""
    import hashlib
    import psutil

    paths = releases.ReleasePaths.for_home(home)
    record = releases._read_txn(paths)
    if not record or not record.get("requires_reload"):
        return False
    expected = Path(record["candidate"] or record["source"]).resolve()
    expected_sha = record["source_sha"] if record["candidate"] is None else expected.name
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            row = json.loads(output.read_text(encoding="utf-8"))
            supervisor = gateway_launchd._launchctl_supervised_pid(plist_path.stem)
            if supervisor and Path(row["cwd"]).resolve() == expected:
                proc = psutil.Process(row["pid"])
                ancestors = {parent.pid for parent in proc.parents()}
                if (supervisor in ancestors or supervisor == proc.pid) and (
                    gateway_pid is None or gateway_pid == proc.pid
                ) and Path(proc.cwd()).resolve() == expected and (
                    Path(proc.exe()).resolve() == releases._interpreter_process_executable(Path(row["exe"]))
                ):
                    releases._verify_transaction(paths, record)
                    body = plist_path.read_bytes()
                    if hashlib.sha256(body).hexdigest() != record["plist"]["intended_sha256"]:
                        return False
                    record["reload_ack"] = {
                        "launchd_pid": supervisor, "gateway_pid": proc.pid,
                        "release_root": str(expected), "code_sha": expected_sha,
                        "plist_sha256": record["plist"]["intended_sha256"],
                    }
                    record["reload_done"] = True
                    releases._write_txn(paths, record)
                    releases._finish_txn(paths, record)
                    return True
        except (OSError, ValueError, KeyError, psutil.Error):
            pass
        time.sleep(.1)
    return False


@pytest.mark.platforms("macos")
def test_two_s2_bearing_releases_rollback_retains_previous(tmp_path, monkeypatch, request):
    home = tmp_path / "profile"
    label = f"ai.hermes.s2spike.{uuid.uuid4().hex}"
    domain = f"gui/{os.getuid()}"
    output = home / "observed.json"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: label)
    monkeypatch.setattr(gateway_launchd, "get_launchd_label", lambda: label)

    from hermes_cli import immutable_releases as releases
    # Both commits contain the real S2 tree. A pre-S2 fixture would be refused by
    # the candidate smoke check and would not prove an installable release.
    source = tmp_path / "source-revisions"
    # Borrow local objects instead of negotiating the entire partial-clone
    # history through upload-pack. Only these two disposable revisions matter.
    subprocess.run(["git", "clone", "--shared", "--quiet",
                    str(Path(__file__).resolve().parents[2]), str(source)],
                   check=True, timeout=60)
    # This source install has no optional features. A real source-owned Python
    # prevents staging from inheriting every extra installed in the test runner.
    venv.EnvBuilder(with_pip=False).create(source / ".venv")
    (source / "probe.py").write_text(
        "import hermes_cli,json,os,pathlib,sys,time\n"
        "p=pathlib.Path(os.environ['S2_PROBE_OUTPUT'])\n"
        "p.write_text(json.dumps({'pid':os.getpid(),'exe':sys.executable,"
        "'cwd':os.getcwd(),'module':hermes_cli.__file__,"
        "'release':pathlib.Path(__file__).with_name('version.txt').read_text().strip()}))\n"
        "time.sleep(120)\n", encoding="utf-8")
    revisions = {}
    for name in ("B", "B-prime"):
        (source / "version.txt").write_text(name, encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "probe.py", "version.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=S2", "-c", "user.email=s2@example.test",
                        "-c", "commit.gpgsign=false", "commit", "--no-verify", "-qm", name], check=True)
        revisions[name] = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    sha_a, sha_b = revisions["B"], revisions["B-prime"]
    for name, sha in revisions.items():
        release, status = releases.stage_release(source, home, sha=sha)
        assert status == "staged" and release == home / "releases" / sha

    promote(home, home / "releases" / sha_a)
    plist = plistlib.loads(gateway.generate_launchd_plist(
        release_target=home / "releases" / sha_a).encode())
    assert plist["Label"] == label
    assert plist["WorkingDirectory"] == str(home / "current")
    assert str(home / "current" / ".venv" / "bin" / "python") in " ".join(plist["ProgramArguments"])
    assert plist["EnvironmentVariables"]["PATH"].split(":")[0] == str(home / "current" / ".venv" / "bin")
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
    probe_plist = path.read_bytes()
    # The updater renders the target definition before rollback. Preserve the
    # probe payload while letting it exercise the real plist transaction/reload.
    monkeypatch.setattr(gateway, "generate_launchd_plist",
                        lambda release_target=None: probe_plist.decode("utf-8"))

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
                        root = home / "releases" / (sha_a if name == "B" else sha_b)
                        for key in ("cwd", "module"):
                            assert Path(result[key]).resolve().is_relative_to(root)
                        assert Path(result["exe"]) == home / "current" / ".venv" / "bin" / "python"
                        return result
            time.sleep(.1)
        raise AssertionError(f"no launchd process for {name}; logs: {list(logs.glob('*'))}")

    target = f"{domain}/{label}"
    register_disposable_label(request, label, path)
    try:
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True, timeout=15)
        a = observed("B")
        promote(home, home / "releases" / sha_b)
        subprocess.run(["launchctl", "kickstart", "-k", target], check=True, timeout=90)
        b = observed("B-prime", a["pid"])
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
        monkeypatch.setattr(releases, "acknowledge_running_release",
                            lambda path_home: _ack_observed_probe(releases, path_home, path, output))
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
        rolled_back = observed("B", b["pid"])
        assert rolled_back["pid"] != b["pid"]
        assert (home / "current").resolve() == home / "releases" / sha_a
        assert (home / "previous").resolve() == home / "releases" / sha_b
        assert path.read_bytes() == probe_plist
        assert (home / "releases" / sha_b).is_dir()
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


@pytest.mark.platforms("macos")
def test_first_migration_a_to_b_rollback_restores_source_revision_and_plist(tmp_path, monkeypatch, request):
    """Updater promotion and reversal reload one throwaway job, never the live label."""
    from hermes_cli import gateway_launchd, immutable_releases as releases, update_cmd, update_receipt

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
        "if pathlib.Path.cwd().name == 'source':\n"
        " from demo_dep import old_api as api\n"
        "else:\n"
        " from demo_dep import new_api as api\n"
        "pathlib.Path(os.environ['S2_PROBE_OUTPUT']).write_text(json.dumps("
        "{'pid': os.getpid(), 'cwd': os.getcwd(), 'exe': sys.executable, "
        "'api': api(), 'release': pathlib.Path(__file__).resolve().parent.name}))\n"
        "time.sleep(120)\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "probe.py", "hermes_cli/__init__.py"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=S2", "-c", "user.email=s2@example.test",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "source"], check=True)
    sha_a = releases.release_sha(source)
    (source / "version.txt").write_text("B\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "version.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=S2", "-c", "user.email=s2@example.test",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "B"], check=True)
    sha_b = releases.release_sha(source)
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(source), "push", "-q", "origin", "HEAD:main"], check=True)
    subprocess.run(["git", "-C", str(source), "reset", "--hard", sha_a], check=True, capture_output=True)
    def install_demo(python, api):
        site = Path(subprocess.check_output([str(python), "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"], text=True).strip())
        (site / "demo_dep.py").write_text(f"def {api}(): return '{api}'\n", encoding="utf-8")
        metadata = site / ("demo_dep-1.0.dist-info" if api == "old_api" else "demo_dep-2.0.dist-info")
        metadata.mkdir()
        (metadata / "METADATA").write_text(f"Metadata-Version: 2.1\nName: demo-dep\nVersion: {'1.0' if api == 'old_api' else '2.0'}\n", encoding="utf-8")
    source_python = source / ".venv/bin/python"
    venv.EnvBuilder(with_pip=False).create(source / ".venv")
    install_probe_process_dependency(source / ".venv")
    install_demo(source_python, "old_api")
    distributions = lambda python: json.loads(subprocess.check_output([
        str(python), "-c", "import importlib.metadata as m,json;print(json.dumps(sorted((d.metadata['Name'],d.version) for d in m.distributions())))"], text=True))
    source_distributions = distributions(source_python)
    source_tree = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD^{tree}"], text=True).strip()
    b = home / "releases" / sha_b
    b.mkdir(parents=True)
    venv.EnvBuilder(with_pip=False).create(b / ".venv")
    install_probe_process_dependency(b / ".venv")
    install_demo(b / ".venv/bin/python", "new_api")
    (b / "probe.py").write_bytes(subprocess.check_output(["git", "-C", str(source), "show", f"{sha_b}:probe.py"]))
    (b / ".release-ready").write_text(sha_b + "\n", encoding="utf-8")
    (b / ".hermes_build_sha").write_text(sha_b + "\n", encoding="utf-8")
    output = home / "observed.json"
    plist_path = tmp_path / f"{label}.plist"

    def definition(root, python):
        return plistlib.dumps({
            "Label": label, "RunAtLoad": True, "KeepAlive": False,
            "WorkingDirectory": str(root),
            "ProgramArguments": [str(python), str(root / "probe.py")],
            "EnvironmentVariables": {"HERMES_HOME": str(home), "S2_PROBE_OUTPUT": str(output)},
        })

    original = definition(source, source_python)
    replacement = definition(home / "current", home / "current" / ".venv/bin/python")
    plist_path.write_bytes(original)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: label)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(gateway, "generate_launchd_plist",
                        lambda release_target=None: replacement.decode("utf-8"))
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

    register_disposable_label(request, label, plist_path)
    try:
        subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True, timeout=15)
        a = observed("source")
        assert a["api"] == "old_api"
        # Fail only the launchd reload callback: the durable intent, plist and
        # pointers move forward, while the live job still runs the old source.
        real_reload = gateway_launchd._reload_installed_launchd_plist
        monkeypatch.setattr(gateway_launchd, "_reload_installed_launchd_plist", lambda path: False)
        update_receipt.begin_update_receipt()
        assert update_cmd._activate_immutable_release() is False
        failed_receipt = update_receipt.finalize_update_receipt("partial")
        assert failed_receipt is not None
        assert (home / "release-txn.json").is_file()
        assert plist_path.read_bytes() == replacement
        assert (home / "current").resolve() == b
        assert (home / "previous").resolve() == source
        assert subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0
        assert json.loads(output.read_text(encoding="utf-8"))["pid"] == a["pid"]
        import psutil
        assert Path(psutil.Process(a["pid"]).cwd()).resolve() == source
        journal = json.loads((home / "release-layout.json").read_text(encoding="utf-8"))
        assert journal["source_sha"] == sha_a
        assert releases.release_sha(source) == sha_a
        assert distributions(source_python) == source_distributions
        assert subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD^{tree}"], text=True).strip() == source_tree
        monkeypatch.setattr(gateway_launchd, "_reload_installed_launchd_plist", real_reload)
        monkeypatch.setattr(releases, "acknowledge_running_release",
                            lambda path_home: _ack_observed_probe(releases, path_home, plist_path, output))
        # The failed callback already has a durable issued marker. An operator
        # explicitly repairs this disposable label after inspecting its old PID;
        # the updater retry must only observe, never invoke reload again.
        assert real_reload(plist_path)
        observed(sha_b, a["pid"])
        repeated = []
        monkeypatch.setattr(gateway_launchd, "_reload_installed_launchd_plist",
                            lambda path: repeated.append(path) or False)
        update_receipt.begin_update_receipt()
        assert update_cmd._activate_immutable_release(sha=sha_b)
        assert repeated == []
        monkeypatch.setattr(gateway_launchd, "_reload_installed_launchd_plist", real_reload)
        updated_receipt = update_receipt.finalize_update_receipt("success")
        assert updated_receipt is not None
        recorded = json.loads(updated_receipt.read_text(encoding="utf-8"))
        # Recovery preserves the operation identity for the receipt: an
        # interrupted migration remains a migration after its reload succeeds.
        assert any(s["name"] == "immutable_release" and "migration=True" in s["detail"]
                   and sha_b in s["detail"] for s in recorded["steps"])
        assert recorded["release_transition"] == {
            "from_sha": sha_a, "to_sha": sha_b, "from_path": str(source),
            "to_path": str(b), "kind": "migration",
        }
        assert (home / "current").resolve() == b
        assert (home / "previous").resolve() == source
        assert plist_path.read_bytes() == replacement
        assert not (home / "release-txn.json").exists()
        promoted = observed(sha_b, a["pid"])
        assert promoted["api"] == "new_api"
        assert releases.release_sha(source) == sha_a
        assert subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD^{tree}"], text=True).strip() == source_tree
        assert distributions(source_python) == source_distributions
        assert Path(promoted["cwd"]).resolve() == b
        assert Path(promoted["exe"]).resolve().is_relative_to(b)
        from types import SimpleNamespace
        monkeypatch.setattr(update_cmd, "_resolve_update_options", lambda *args: SimpleNamespace())
        def restart_throwaway(*args, **kwargs):
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
        assert not (home / "current").exists()
        assert not (home / "previous").exists()
        assert json.loads((home / "release-layout.json").read_text(encoding="utf-8"))["state"] == "rolled-back"
        original_plist = releases.migration_plist(home)
        assert original_plist is not None
        saved_path, saved_body = original_plist
        assert saved_path == plist_path and saved_body == original
        restored = observed("source", promoted["pid"])
        assert restored["api"] == "old_api"
        assert distributions(source_python) == source_distributions
        assert subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD^{tree}"], text=True).strip() == source_tree
        assert restored["pid"] != a["pid"]
        assert Path(restored["cwd"]).resolve() == source
        assert releases.release_sha(source) == sha_a
        assert plist_path.read_bytes() == original
        assert b.is_dir()
        rollback_receipt = json.loads((home / "logs/update_receipts/latest.json").read_text(encoding="utf-8"))
        assert rollback_receipt["outcome"] == "success"
        assert rollback_receipt["release_transition"] == {
            "from_sha": sha_b, "to_sha": sha_a, "from_path": str(b),
            "to_path": str(source), "kind": "migration_reversal",
        }
        assert any(s["name"] == "immutable_rollback" and s["ok"] for s in rollback_receipt["steps"])
    finally:
        subprocess.run(["launchctl", "bootout", target], capture_output=True, timeout=15)
        assert subprocess.run(["launchctl", "print", target], capture_output=True).returncode != 0
