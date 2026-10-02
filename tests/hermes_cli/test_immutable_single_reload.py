"""A release transaction must not relaunch its acknowledged launchd gateway twice."""
import json
import os
import plistlib
import subprocess
import sys
import threading
import time
import uuid
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import gateway, gateway_launchd, immutable_releases as releases, update_cmd, update_cmd_fleet, update_receipt
from tests.hermes_cli.immutable_launchd_cleanup import register_disposable_label, sweep_prior_sessions, install_probe_process_dependency


@pytest.fixture(scope="module", autouse=True)
def _sweep_disposable_jobs(request):
    sweep_prior_sessions(request)


@pytest.fixture
def release_job(tmp_path, monkeypatch, request):
    home = tmp_path / "profile"
    home.mkdir()
    label = f"ai.hermes.s2spike.{uuid.uuid4().hex}"
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{label}"
    output = home / "observed.json"
    log = tmp_path / "launchctl.log"
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "launchctl").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$S2_LAUNCHCTL_LOG"\nexec /bin/launchctl "$@"\n', encoding="utf-8")
    (shim / "launchctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    monkeypatch.setenv("S2_LAUNCHCTL_LOG", str(log))
    monkeypatch.setenv("HERMES_HOME", str(home))
    a, b = (home / "releases" / (c * 40) for c in ("a", "b"))
    for root in (a, b):
        root.mkdir(parents=True)
        venv.EnvBuilder(with_pip=False).create(root / ".venv")
        install_probe_process_dependency(root / ".venv")
        (root / ".release-ready").write_text(root.name + "\n")
        (root / ".hermes_build_sha").write_text(root.name + "\n")
        (root / "probe.py").write_text(
            "import json,os,pathlib,sys,time\n"
            "pathlib.Path(os.environ['S2_PROBE_OUTPUT']).write_text(json.dumps("
            "{'pid':os.getpid(),'cwd':os.getcwd(),'exe':sys.executable}))\n"
            "time.sleep(120)\n", encoding="utf-8")
    releases.promote(home, a)
    plist_path = tmp_path / f"{label}.plist"
    def definition():
        return plistlib.dumps({
            "Label": label, "RunAtLoad": True, "KeepAlive": False,
            "WorkingDirectory": str(home / "current"),
            "ProgramArguments": [str(home / "current/.venv/bin/python"), str(home / "current/probe.py")],
            "EnvironmentVariables": {"HERMES_HOME": str(home), "S2_PROBE_OUTPUT": str(output)},
        })
    plist_path.write_bytes(definition())
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: label)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(gateway, "generate_launchd_plist", lambda release_target=None: definition().decode())
    monkeypatch.setattr(gateway, "_launchd_domain", lambda: domain)
    monkeypatch.setattr(gateway, "launchd_gateway_labels_for_install", lambda: [label])
    monkeypatch.setattr(gateway, "legacy_launchd_labels_for_install", lambda **kw: [])
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kw: [])
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **kw: [])
    monkeypatch.setattr(gateway_launchd, "get_launchd_label", lambda: label)
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"immutable_releases": True, "release_acknowledgement_timeout_seconds": 15})
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(releases, "stage_release", lambda *args, sha=None, **kw: (home / "releases" / sha, "existing"))
    monkeypatch.setattr(gateway, "_wait_for_api_server_port_free", lambda: None)
    monkeypatch.setattr(update_cmd_fleet, "_restart_systemd_gateway_units", lambda *args: None)
    monkeypatch.setattr(update_cmd_fleet, "_restart_manual_gateways", lambda *args: None)
    monkeypatch.setattr(update_cmd_fleet, "_force_kill_stuck_gateways", lambda *args: None)
    monkeypatch.setattr(update_cmd_fleet, "_print_legacy_units_warning", lambda: None)
    monkeypatch.setattr(update_cmd, "_finish_dashboard_update_cleanup", lambda *args, **kw: None)
    monkeypatch.setattr(update_cmd, "_surviving_pre_update_serve_runtimes", lambda *args: [])
    monkeypatch.setattr(update_cmd._m(), "_fleet_probe_expected_runtimes", lambda *args: True)
    monkeypatch.setattr(update_receipt, "_profile_homes", lambda: [("default", home)])
    def socket_identity(_home):
        import psutil
        try:
            row = json.loads(output.read_text())
            proc = psutil.Process(row["pid"])
            root = Path(row["cwd"]).resolve()
            if proc.is_running() and Path(proc.cwd()).resolve() == root:
                return row["pid"], {"code_sha": root.name}
        except (OSError, ValueError, psutil.Error):
            pass
        return None
    monkeypatch.setattr(update_receipt, "_socket_identity", socket_identity)
    monkeypatch.setattr(update_receipt, "_gateway_code_root", lambda pid, _home: Path(json.loads(output.read_text())["cwd"]).resolve())
    register_disposable_label(request, label, plist_path)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True, timeout=15)
    def observed(root, old_pid=None):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                row = json.loads(output.read_text())
                if row["pid"] != old_pid and Path(row["cwd"]).resolve() == root:
                    return row
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(.1)
        raise AssertionError(f"gateway did not reach {root}: {output.read_text() if output.exists() else 'absent'}")
    initial = observed(a)
    log.write_text("")
    yield SimpleNamespace(home=home, a=a, b=b, label=label, path=plist_path,
                          output=output, log=log, observed=observed, initial=initial)
    subprocess.run(["launchctl", "bootout", target], capture_output=True, timeout=15)
    assert subprocess.run(["launchctl", "print", target], capture_output=True).returncode != 0


def _ack_after_spawn(job, root, old_pid):
    """A disposable probe stands in for the gateway's status-transition ACK."""
    def acknowledge():
        job.observed(root, old_pid)
        from tests.hermes_cli.test_immutable_launchd_real import _ack_observed_probe
        assert _ack_observed_probe(releases, job.home, job.path, job.output)
    worker = threading.Thread(target=acknowledge, daemon=True)
    worker.start()
    return worker


def _assert_one_reload(job, old_pid, root):
    row = job.observed(root, old_pid)
    calls = job.log.read_text().splitlines()
    mutations = [line for line in calls if line.startswith(("bootout ", "bootstrap ", "kickstart "))
                 and job.label in line]
    assert [line.split()[0] for line in mutations] == ["bootout", "bootstrap"], mutations
    assert row["pid"] != old_pid
    receipt = json.loads((job.home / "logs/update_receipts/latest.json").read_text())
    assert receipt["outcome"] == "success"
    assert receipt["gateway_restart"]["restarted_services"] == [job.label]
    assert len(receipt["fleet"]) == 1
    assert receipt["fleet"][0]["state"] == "current"
    assert receipt["fleet"][0]["code_sha"] == root.name
    assert Path(receipt["fleet"][0]["code_root"]).resolve() == root
    return row


@pytest.mark.platforms("macos")
def test_release_to_release_activation_reload_once(release_job, monkeypatch):
    job = release_job
    monkeypatch.setattr(update_cmd, "_require_immutable_launchd", lambda: None)
    worker = _ack_after_spawn(job, job.b, job.initial["pid"])
    update_receipt.begin_update_receipt()
    assert update_cmd._activate_immutable_release(sha=job.b.name, source=job.a)
    worker.join(timeout=5)
    outcome = update_cmd._restart_gateway_fleet_after_update(
        None, False, acknowledged_release_root=job.b)
    update_cmd._verify_fleet_after_update(outcome, _pre_update_plan=None,
        _windows_gateway_resume=None, node_failures=[], update_complete=True,
        expected_sha=job.b.name, expected_root=job.b)
    _assert_one_reload(job, job.initial["pid"], job.b)


@pytest.mark.platforms("macos")
def test_historical_ack_cannot_credit_dead_gateway(release_job, monkeypatch):
    job = release_job
    monkeypatch.setattr(update_cmd, "_require_immutable_launchd", lambda: None)
    worker = _ack_after_spawn(job, job.b, job.initial["pid"])
    assert update_cmd._activate_immutable_release(sha=job.b.name, source=job.a)
    worker.join(timeout=5)
    assert update_cmd_fleet._acknowledged_release_launchd_label(job.home, job.b) == job.label
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{job.label}"], check=True)
    assert update_cmd_fleet._acknowledged_release_launchd_label(job.home, job.b) is None
    attempted = []
    monkeypatch.setattr(update_cmd_fleet, "_restart_launchd_gateway_after_update",
                        lambda **kwargs: attempted.append(kwargs) or ([], [job.label]))
    credited, failed = [], []
    update_cmd_fleet._restart_macos_launchd_gateways(
        credited, failed, 0, acknowledged_label=update_cmd_fleet._acknowledged_release_launchd_label(job.home, job.b))
    assert attempted and not credited and failed == [job.label]


@pytest.mark.platforms("macos")
def test_acknowledged_release_catchup_verifies_without_second_relaunch(release_job, monkeypatch):
    job = release_job
    monkeypatch.setattr(update_cmd, "_require_immutable_launchd", lambda: None)
    worker = _ack_after_spawn(job, job.b, job.initial["pid"])
    update_receipt.begin_update_receipt()
    assert update_cmd._activate_immutable_release(sha=job.b.name, source=job.a)
    worker.join(timeout=5)
    monkeypatch.setattr(update_cmd_fleet, "_pending_fleet_restart_needed", lambda: True)
    update_cmd._apply_pending_fleet_restart_catchup()
    _assert_one_reload(job, job.initial["pid"], job.b)


@pytest.mark.platforms("macos")
def test_stale_credited_gateway_gets_one_relaunch(release_job, monkeypatch):
    job = release_job
    monkeypatch.setattr(update_cmd, "_require_immutable_launchd", lambda: None)
    worker = _ack_after_spawn(job, job.b, job.initial["pid"])
    assert update_cmd._activate_immutable_release(sha=job.b.name, source=job.a)
    worker.join(timeout=5)
    job.log.write_text("")
    monkeypatch.setattr(update_cmd_fleet, "_pending_fleet_restart_needed", lambda: True)
    identity = update_receipt._socket_identity
    def stale_until_relaunch(home):
        observed = identity(home)
        if observed and not any(line.startswith(("bootout ", "kickstart "))
                                for line in job.log.read_text().splitlines()):
            pid, status = observed
            return pid, {**status, "code_sha": job.a.name}
        return observed
    monkeypatch.setattr(update_receipt, "_socket_identity", stale_until_relaunch)
    update_receipt.begin_update_receipt()
    update_cmd._apply_pending_fleet_restart_catchup()
    row = job.observed(job.b, job.initial["pid"])
    assert row["pid"] != job.initial["pid"]
    mutations = [line for line in job.log.read_text().splitlines()
                 if line.startswith(("bootout ", "bootstrap ", "kickstart ")) and job.label in line]
    assert [line.split()[0] for line in mutations] == ["kickstart"]
    assert update_receipt.read_latest_receipt()["outcome"] == "success"


@pytest.mark.platforms("macos")
def test_release_to_release_rollback_reload_once(release_job):
    job = release_job
    releases.promote(job.home, job.b)
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{job.label}"], check=True)
    prior = job.observed(job.b, job.initial["pid"])
    job.log.write_text("")
    worker = _ack_after_spawn(job, job.a, prior["pid"])
    update_cmd._cmd_update_impl(SimpleNamespace(rollback=True), gateway_mode=False)
    worker.join(timeout=5)
    _assert_one_reload(job, prior["pid"], job.a)


@pytest.mark.platforms("macos")
def test_pending_then_acknowledged_recovery_does_not_relaunch(release_job, monkeypatch):
    job = release_job
    monkeypatch.setattr(update_cmd, "_require_immutable_launchd", lambda: None)
    # An issued reload with no ACK is pending; the retry must observe the existing job.
    monkeypatch.setattr(update_cmd, "_release_acknowledgement_timeout", lambda: 0.0)
    update_receipt.begin_update_receipt()
    assert not update_cmd._activate_immutable_release(sha=job.b.name, source=job.a)
    assert (job.home / "release-txn.json").exists()
    job.observed(job.b, job.initial["pid"])
    from tests.hermes_cli.test_immutable_launchd_real import _ack_observed_probe
    assert _ack_observed_probe(releases, job.home, job.path, job.output)
    monkeypatch.setattr(update_cmd, "_release_acknowledgement_timeout", lambda: 15.0)
    assert update_cmd._activate_immutable_release(sha=job.b.name, source=job.a)
    outcome = update_cmd._restart_gateway_fleet_after_update(
        None, False, acknowledged_release_root=job.b)
    update_cmd._verify_fleet_after_update(outcome, _pre_update_plan=None,
        _windows_gateway_resume=None, node_failures=[], update_complete=True,
        expected_sha=job.b.name, expected_root=job.b)
    _assert_one_reload(job, job.initial["pid"], job.b)
