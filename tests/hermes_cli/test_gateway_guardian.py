"""Guardian decisions at the launchd and immutable-release boundary."""
import fcntl
import json
import os
import plistlib
import subprocess
import time
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
            is_loaded = state["loaded"]
            return subprocess.CompletedProcess(argv, 0 if is_loaded else 113, stdout="pid = 123\n" if is_loaded else "", stderr="Could not find service" if not is_loaded else "")
        if argv[1] == "bootstrap":
            state["loaded"] = True
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(guardian.subprocess, "run", run)
    return calls


@pytest.mark.macos_only
def test_unloaded_service_bootstraps_once_and_records_receipt(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    calls = fake_launchctl(monkeypatch, label)
    monkeypatch.setattr(guardian, "healthy", lambda *args: True)
    assert guardian.run_once(home, plist, label, grace=0) == "repaired"
    assert [row[1] for row in calls] == ["print", "bootstrap", "print"]
    assert any(json.loads(path.read_text(encoding="utf-8"))["outcome"] == "repaired"
               for path in (home / "logs/guardian").glob("*.json"))


@pytest.mark.macos_only
def test_stop_marker_never_fights_unloaded_service(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    calls = fake_launchctl(monkeypatch, label)
    guardian.set_intent(home, stopped=True)
    assert guardian.run_once(home, plist, label) == "stopped"
    assert not calls


@pytest.mark.macos_only
def test_loaded_service_does_not_bootstrap(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    calls = fake_launchctl(monkeypatch, label, loaded=True)
    monkeypatch.setattr(guardian, "healthy", lambda *args: True)
    assert guardian.run_once(home, plist, label) == "healthy"
    assert [row[1] for row in calls] == ["print"]


@pytest.mark.macos_only
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


@pytest.mark.macos_only
def test_corrupt_current_pointer_reports_without_source_fallback(tmp_path, monkeypatch):
    home, plist, label, a, b = layout(tmp_path)
    (home / "current").unlink()
    (home / "current").symlink_to(home / "missing")
    calls = fake_launchctl(monkeypatch, label)
    assert guardian.run_once(home, plist, label) == "alert"
    assert not calls


@pytest.mark.macos_only
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


@pytest.mark.macos_only
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


@pytest.mark.macos_only
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
    assert guardian.run_once(home, plist, label, grace=0) == "rolled_back"
    assert done == [True]
    (a / ".release-ready").unlink()
    done.clear()
    assert guardian.run_once(home, plist, label, grace=0) == "alert"
    assert done == []
