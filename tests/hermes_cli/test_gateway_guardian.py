"""Guardian decisions at the launchd and immutable-release boundary."""
import fcntl
import json
import os
import plistlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_cli import gateway_guardian as guardian


def layout(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    label = "ai.hermes.test.guardian"
    plist = tmp_path / f"{label}.plist"
    plist.write_bytes(plistlib.dumps({"Label": label, "EnvironmentVariables": {"HERMES_HOME": str(home)},
                                     "WorkingDirectory": str(home / "current")}))
    a, b = (home / "releases" / name for name in ("a" * 40, "b" * 40))
    for release in (a, b):
        release.mkdir(parents=True)
        for marker in (".release-ready", ".hermes_build_sha"):
            (release / marker).write_text(release.name + "\n", encoding="utf-8")
    (home / "current").symlink_to(b)
    (home / "previous").symlink_to(a)
    return home, plist, label, a, b


def fake_launchctl(monkeypatch, label, *, loaded=False):
    calls = []
    state = {"loaded": loaded}
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "print":
            is_loaded = state["loaded"] and argv[2].startswith(f"gui/{os.getuid()}/")
            return subprocess.CompletedProcess(argv, 0 if is_loaded else 113, stdout="pid = 123\n" if is_loaded else "", stderr="Could not find service" if not is_loaded else "")
        if argv[1] == "managername":
            return subprocess.CompletedProcess(argv, 0, stdout="Aqua", stderr="")
        if argv[1] == "bootstrap":
            state["loaded"] = True
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(guardian.subprocess, "run", run)
    return calls

@pytest.mark.platforms("macos")
def test_explicit_grace_does_not_load_user_config(tmp_path, monkeypatch):
    home, plist, label, *_ = layout(tmp_path)
    (home / "config.yaml").write_text("updates:\n  release_acknowledgement_timeout_seconds: 180\n")
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective",
                        lambda *args, **kwargs: pytest.fail("flag-off config loaded"))
    monkeypatch.setattr(guardian, "_run", lambda *args, **kwargs: "healthy")
    assert guardian.run_once(home, plist, label, grace=12) == "healthy"


@pytest.mark.platforms("macos")
def test_explicit_grace_ignores_unrelated_invalid_config(tmp_path, monkeypatch):
    home, plist, label, *_ = layout(tmp_path)
    (home / "config.yaml").write_text("updates: [unclosed\n")
    monkeypatch.setattr(guardian, "_run", lambda *args, **kwargs: "healthy")
    assert guardian.run_once(home, plist, label, grace=12) == "healthy"


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("value", ["text", 0, -1, ".nan", ".inf"])
def test_invalid_grace_writes_alert_receipt(tmp_path, value, monkeypatch):
    home, plist, label, *_ = layout(tmp_path)
    monkeypatch.setattr(guardian, "_run", lambda *args, **kwargs: "healthy")
    (home / "config.yaml").write_text(f"updates:\n  release_acknowledgement_timeout_seconds: {value}\n")
    assert guardian.run_once(home, plist, label) == "alert"
    assert any(json.loads(path.read_text())["outcome"] == "alert"
               for path in (home / "logs/guardian").glob("*.json"))

@pytest.mark.platforms("macos")
def test_invalid_yaml_writes_alert_receipt(tmp_path):
    home, plist, label, *_ = layout(tmp_path)
    (home / "config.yaml").write_text("updates: [unclosed\n")
    assert guardian.run_once(home, plist, label) == "alert"
    assert any(json.loads(path.read_text())["outcome"] == "alert"
               for path in (home / "logs/guardian").glob("*.json"))


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("grace", [0, -1, float("nan"), float("inf"), "invalid"])
def test_explicit_invalid_grace_writes_alert_receipt(tmp_path, monkeypatch, grace):
    home, plist, label, *_ = layout(tmp_path)
    monkeypatch.setattr(guardian, "_run", lambda *args, **kwargs: "healthy")
    assert guardian.run_once(home, plist, label, grace=grace) == "alert"
    assert any(json.loads(path.read_text())["outcome"] == "alert"
               for path in (home / "logs/guardian").glob("*.json"))


@pytest.mark.platforms("macos")
def test_unrelated_invalid_update_key_does_not_block_repair(tmp_path, monkeypatch):
    home, plist, label, *_ = layout(tmp_path)
    (home / "config.yaml").write_text("updates:\n  immutable_releases: not-a-boolean\n")
    calls = fake_launchctl(monkeypatch, label)
    monkeypatch.setattr(guardian, "healthy", lambda *args: True)
    assert guardian.run_once(home, plist, label) == "repaired"
    assert [row[1] for row in calls].count("bootstrap") == 1

@pytest.mark.platforms("macos")
def test_unloaded_service_bootstraps_once_and_records_receipt(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    calls = fake_launchctl(monkeypatch, label)
    monkeypatch.setattr(guardian, "healthy", lambda *args: True)
    assert guardian.run_once(home, plist, label, grace=0.000001) == "repaired"
    assert [row[1] for row in calls].count("bootstrap") == 1
    assert calls[-1][1] == "print"
    assert any(json.loads(path.read_text(encoding="utf-8"))["outcome"] == "repaired"
               for path in (home / "logs/guardian").glob("*.json"))


@pytest.mark.platforms("macos")
def test_stop_marker_never_fights_unloaded_service(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    calls = fake_launchctl(monkeypatch, label)
    guardian.set_intent(home, stopped=True)
    assert guardian.run_once(home, plist, label) == "stopped"
    assert not calls


@pytest.mark.platforms("macos")
def test_stop_intent_short_circuits_malformed_config(tmp_path, monkeypatch):
    home, plist, label, *_ = layout(tmp_path)
    guardian.set_intent(home, stopped=True)
    (home / "config.yaml").write_text("updates: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective",
                        lambda *args, **kwargs: pytest.fail("stopped intent loaded config"))
    assert guardian.run_once(home, plist, label) == "stopped"


@pytest.mark.platforms("macos")
def test_loaded_service_does_not_bootstrap(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    calls = fake_launchctl(monkeypatch, label, loaded=True)
    monkeypatch.setattr(guardian, "healthy", lambda *args: True)
    assert guardian.run_once(home, plist, label) == "healthy"
    assert not any(row[1] in {"bootstrap", "bootout"} for row in calls)


@pytest.mark.platforms("macos")
def test_attempt_cap_and_lock_prevent_repair(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    calls = fake_launchctl(monkeypatch, label)
    for n in range(3):
        guardian.receipt(home, "bootstrap", "attempt", label=label)
    assert guardian.run_once(home, plist, label) == "capped"
    assert not any(row[1] == "bootstrap" for row in calls)
    path = home / "logs/guardian/guardian.lock"
    with path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert guardian.run_once(home, plist, label) == "locked"


@pytest.mark.platforms("macos")
def test_corrupt_current_pointer_reports_without_source_fallback(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    (home / "current").unlink()
    (home / "current").symlink_to(home / "missing")
    calls = fake_launchctl(monkeypatch, label)
    assert guardian.run_once(home, plist, label) == "alert"
    assert not calls


@pytest.mark.platforms("macos")
def test_source_checkout_plist_is_never_bootstrapped(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    definition = plistlib.loads(plist.read_bytes())
    definition["WorkingDirectory"] = str(tmp_path / "source")
    plist.write_bytes(plistlib.dumps(definition))
    calls = fake_launchctl(monkeypatch, label)
    assert guardian.run_once(home, plist, label) == "alert"
    assert not calls


@pytest.mark.platforms("macos")
def test_pending_failed_switch_archived_before_rollback(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    txn = {"version": 1, "operation": "promote", "candidate": str(b),
           "previous_intended": str(a), "current_original": str(a),
           "previous_original": str(b), "requires_reload": True,
           "reload_issued": {"at": "2020-01-01T00:00:00+00:00"}}
    pending = home / "release-txn.json"
    pending.write_text(json.dumps(txn), encoding="utf-8")
    from hermes_cli.immutable_releases import abandon_failed_switch
    abandon_failed_switch(home, candidate=b, previous=a)
    assert not pending.exists()
    assert any(json.loads(path.read_text(encoding="utf-8")) == txn
               for path in home.glob("release-abandoned-*.json"))


@pytest.mark.platforms("macos")
def test_acknowledged_or_mismatched_switch_is_not_abandoned(tmp_path):
    home, plist, label, a, b = layout(tmp_path)
    from hermes_cli.immutable_releases import abandon_failed_switch
    txn = {"version": 1, "operation": "promote", "candidate": str(b),
           "previous_intended": str(a), "current_original": str(a),
           "previous_original": str(b), "reload_issued": {"at": "2020-01-01T00:00:00+00:00"},
           "reload_ack": {"gateway_pid": 10}}
    pending = home / "release-txn.json"
    pending.write_text(json.dumps(txn), encoding="utf-8")
    with pytest.raises(RuntimeError):
        abandon_failed_switch(home, candidate=b, previous=a)
    assert pending.exists()


@pytest.mark.platforms("macos")
def test_switch_in_grace_or_acknowledged_does_not_rollback(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    calls = fake_launchctl(monkeypatch, label, loaded=True)
    monkeypatch.setattr(guardian, "healthy", lambda *args: False)
    txn = {"version": 1, "operation": "promote", "candidate": str(b),
           "previous_intended": str(a), "reload_issued": {"at": datetime.now(timezone.utc).isoformat()}}
    last = home / "release-last-txn.json"
    last.write_text(json.dumps(txn), encoding="utf-8")
    monkeypatch.setattr(guardian, "rollback_switch", lambda *args, **kwargs: pytest.fail("premature rollback"))
    assert guardian.run_once(home, plist, label, grace=180) == "waiting"
    txn["reload_ack"] = {"gateway_pid": 123}
    last.write_text(json.dumps(txn), encoding="utf-8")
    assert guardian.run_once(home, plist, label, grace=0.000001) == "waiting"
    assert not any(row[1] in {"bootstrap", "bootout"} for row in calls)


@pytest.mark.platforms("macos")
def test_configured_release_ack_grace_delays_guardian_rollback(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    (home / "config.yaml").write_text(
        "updates:\n  release_acknowledgement_timeout_seconds: 300\n", encoding="utf-8")
    (home / "release-last-txn.json").write_text(json.dumps({
        "version": 1, "operation": "promote", "candidate": str(b),
        "previous_intended": str(a),
        "reload_issued": {"at": datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 240, timezone.utc).isoformat()},
    }), encoding="utf-8")
    calls = fake_launchctl(monkeypatch, label)
    assert guardian.run_once(home, plist, label) == "waiting"
    assert calls == []


@pytest.mark.platforms("macos")
def test_stale_runtime_status_is_not_healthy(tmp_path, monkeypatch):
    from hermes_cli import gateway_launchd
    import psutil
    home, plist, label, a, b = layout(tmp_path)
    (home / "gateway_state.json").write_text(json.dumps({
        "pid": os.getpid(), "gateway_state": "running", "code_sha": b.name,
        "updated_at": "2020-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    monkeypatch.setattr(gateway_launchd, "_launchctl_supervised_pid", lambda name: os.getpid())
    monkeypatch.setattr(psutil.Process, "cwd", lambda self: str(b))
    assert not guardian.healthy(home, label, b)


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("operation", ["promote", "first-migration"])
def test_unloaded_pending_reload_waits_until_grace_expires(tmp_path, monkeypatch, operation):
    home, plist, label, a, b = layout(tmp_path)
    calls = fake_launchctl(monkeypatch, label)
    monkeypatch.setattr(guardian, "healthy", lambda *args: True)
    txn = {"version": 1, "operation": operation, "candidate": str(b),
           "previous_intended": str(a), "reload_issued": {"at": datetime.now(timezone.utc).isoformat()}}
    (home / "release-last-txn.json").write_text(json.dumps(txn))
    assert guardian.run_once(home, plist, label, grace=180, domain="gui/501") == "waiting"
    assert not any(row[1] == "bootstrap" for row in calls)
    monkeypatch.setattr(guardian, "rollback_switch", lambda *args, **kwargs: True)
    assert guardian.run_once(home, plist, label, grace=0.000001, domain=f"gui/{os.getuid()}") == "rolled_back"
    assert not any(row[1] == "bootstrap" for row in calls)


@pytest.mark.platforms("macos")
def test_gateway_restart_command_clears_stopped_intent(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import gateway
    from hermes_cli import gateway_profile_lifecycle
    home, *_ = layout(tmp_path)
    guardian.set_intent(home, stopped=True)
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "_refuse_from_inside_gateway", lambda *args: None)
    monkeypatch.setattr(gateway_profile_lifecycle, "profile_lifecycle", lambda *args: False)
    monkeypatch.setattr(gateway, "_guard_named_profile_under_multiplexer", lambda **kwargs: None)
    dispatched = []
    monkeypatch.setattr(gateway, "_dispatch_via_service_manager_if_s6",
                        lambda action: dispatched.append(action) or True)
    gateway._cmd_restart(SimpleNamespace(system=False, all=False, force=False))
    assert dispatched == ["restart"]
    assert not guardian.intent_path(home).exists()


@pytest.mark.platforms("macos")
def test_launchd_restart_clears_stopped_intent(tmp_path, monkeypatch):
    from hermes_cli import gateway, gateway_launchd
    from gateway import status
    home, *_ = layout(tmp_path)
    guardian.set_intent(home, stopped=True)
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: "ai.hermes.test.guardian")
    monkeypatch.setattr(gateway, "_launchd_domain", lambda: f"gui/{os.getuid()}")
    monkeypatch.setattr(status, "get_running_pid", lambda: 123)
    monkeypatch.setattr(gateway, "_request_gateway_self_restart", lambda pid: True)
    gateway_launchd.launchd_restart()
    assert not guardian.intent_path(home).exists()


@pytest.mark.platforms("macos")
def test_rollback_waits_for_old_pid_and_recovers_bootstrap_eio(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    calls = []
    from hermes_cli import gateway_launchd
    import psutil
    monkeypatch.setattr(gateway_launchd, "_launchctl_supervised_pid", lambda name: 123)
    class Previous:
        def __init__(self, pid):
            assert pid == 123
        def wait(self, timeout):
            calls.append("drained")
    monkeypatch.setattr(psutil, "Process", Previous)
    def run(argv, **kwargs):
        calls.append(argv[1])
        if argv[1] == "bootstrap" and calls.count("bootstrap") == 1:
            raise subprocess.CalledProcessError(5, argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(guardian.subprocess, "run", run)
    monkeypatch.setattr(guardian, "rollback", lambda *args, **kwargs:
                        kwargs["reload_callback"]() and {"reload_pending": False})
    monkeypatch.setattr(guardian, "healthy", lambda *args: True)
    assert guardian.rollback_switch(home, plist, label, a, domain=f"gui/{os.getuid()}")
    assert calls[:4] == ["bootout", "drained", "bootstrap", "bootout"]
    assert calls[-1] == "bootstrap"


@pytest.mark.platforms("macos")
def test_repeated_alerts_are_deduplicated_and_old_receipts_pruned(tmp_path):
    home, *_ = layout(tmp_path)
    old = guardian.receipt(home, "inspect", "alert", reason="old")
    os.utime(old, (0, 0))
    first = guardian.receipt(home, "inspect", "alert", reason="persistent")
    second = guardian.receipt(home, "inspect", "alert", reason="persistent")
    assert first == second
    assert not old.exists()
    assert guardian._repair_count(home) == 0


@pytest.mark.platforms("macos")
def test_healthy_rollback_is_acknowledged_on_subsequent_run(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    fake_launchctl(monkeypatch, label, loaded=True)
    monkeypatch.setattr(guardian, "healthy", lambda *args: True)
    pending = home / "release-txn.json"
    pending.write_text(json.dumps({"version": 1, "operation": "rollback", "candidate": str(b)}))
    from hermes_cli import immutable_releases
    def acknowledge(path):
        assert path == home
        pending.unlink()
        return True
    monkeypatch.setattr(immutable_releases, "acknowledge_running_release", acknowledge)
    assert guardian.run_once(home, plist, label) == "healthy"
    assert not pending.exists()


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("domain", ["gui", "user"])
def test_guardian_resolves_existing_gateway_domain(tmp_path, monkeypatch, domain):
    home, plist, label, a, b = layout(tmp_path)
    import os
    selected = f"{domain}/{os.getuid()}"
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "print":
            found = argv[2] == f"{selected}/{label}"
            return subprocess.CompletedProcess(argv, 0 if found else 113,
                stdout="pid = 123" if found else "", stderr="" if found else "Could not find service")
        if argv[1] == "managername":
            return subprocess.CompletedProcess(argv, 0, stdout="Aqua", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(guardian.subprocess, "run", run)
    monkeypatch.setattr(guardian, "healthy", lambda *args: True)
    assert guardian.run_once(home, plist, label) == "healthy"
    assert all(row[1] not in {"bootstrap", "bootout"} for row in calls)
    assert [row[2] for row in calls if row[1] == "print"][-1] == f"{selected}/{label}"


@pytest.mark.platforms("macos")
def test_user_domain_stays_healthy_when_gui_probe_errors(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "print" and argv[2].startswith(f"gui/{os.getuid()}/"):
            return subprocess.CompletedProcess(argv, 5, stdout="", stderr="Input/output error")
        return subprocess.CompletedProcess(argv, 0, stdout="pid = 123", stderr="")
    monkeypatch.setattr(guardian.subprocess, "run", run)
    monkeypatch.setattr(guardian, "healthy", lambda *args: True)
    assert guardian.run_once(home, plist, label) == "healthy"
    assert not any(row[1] in {"bootstrap", "bootout"} for row in calls)


@pytest.mark.platforms("macos")
def test_unknown_launchctl_failure_is_alert_not_bootstrap(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 113, stdout="", stderr="Input/output error")
    monkeypatch.setattr(guardian.subprocess, "run", run)
    assert guardian.run_once(home, plist, label) == "alert"
    assert len(calls) == 2 and all(row[1] == "print" for row in calls)


@pytest.mark.platforms("macos")
def test_failed_switch_rolls_back_only_verified_previous(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    txn = {"version": 1, "operation": "promote", "candidate": str(b), "previous_intended": str(a),
           "current_original": str(a), "previous_original": str(b), "requires_reload": True,
           "reload_issued": {"at": "2020-01-01T00:00:00+00:00"}}
    (home / "release-last-txn.json").write_text(json.dumps(txn))
    calls = fake_launchctl(monkeypatch, label, loaded=True)
    monkeypatch.setattr(guardian, "healthy", lambda *args: False)
    done = []
    monkeypatch.setattr(guardian, "rollback_switch", lambda *args, **kwargs: done.append(True) or True)
    assert guardian.run_once(home, plist, label, grace=0.000001) == "rolled_back"
    assert done == [True]
    (a / ".release-ready").unlink()
    done.clear()
    assert guardian.run_once(home, plist, label, grace=0.000001) == "alert"
    assert done == []
