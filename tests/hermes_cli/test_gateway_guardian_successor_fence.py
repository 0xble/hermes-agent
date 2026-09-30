"""Non-live launchd fencing contracts and takeover regression for PR #266."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from gateway import status
from gateway.generation import GenerationCoordinator, GenerationIdentity
from hermes_cli import gateway_generation_status, gateway_guardian as guardian
from hermes_cli import gateway_overlap


def _failed_overlap(home, monkeypatch):
    pid = os.getpid()
    monkeypatch.setattr(status, "_pid_exists", lambda candidate: candidate == pid)
    coordinator = GenerationCoordinator(home)
    predecessor = GenerationIdentity.create(
        release_sha="a" * 40, label="ai.hermes.test.fence-a", pid=pid,
        start_fingerprint=f"{pid}:{status._get_process_start_time(pid)}")
    failed = GenerationIdentity.create(
        release_sha="b" * 40, label="ai.hermes.test.fence-b", pid=456)
    coordinator.register(predecessor, state="serving")
    coordinator.register(failed, state="ready")
    epoch = coordinator.acquire_lease("active_generation", predecessor.id)
    coordinator.request_transfer(predecessor.id, failed.id, epoch, set())
    coordinator.commit_transfer(predecessor.id, failed.id, epoch)
    (home / "config.yaml").write_text(
        "gateway:\n  overlap_handover:\n    enabled: true\n", encoding="utf-8")
    return coordinator, predecessor, failed


@pytest.mark.macos_only
@pytest.mark.parametrize("scenario", ["loaded", "unloaded", "stubborn", "bootout-error",
                                      "foreign-home", "missing-home", "stale-release", "wrong-label",
                                      "stale-boot", "reused-live-pid"])
def test_failed_successor_fence_requires_unloaded_label_before_rollback(tmp_path, monkeypatch, scenario):
    _coordinator, predecessor, failed = _failed_overlap(tmp_path, monkeypatch)
    if scenario in {"stale-boot", "reused-live-pid"}:
        with _coordinator.connect() as db:
            if scenario == "stale-boot":
                db.execute("UPDATE generations SET boot_id='previous-boot' WHERE id=?", (failed.id,))
            else:
                db.execute("UPDATE generations SET pid=? WHERE id=?", (predecessor.pid, failed.id))
    loaded = scenario != "unloaded"
    clock = [0.0]
    calls = []
    rollbacks = []
    monkeypatch.setattr(guardian, "_gateway_domain", lambda *_: "gui/501")
    monkeypatch.setattr(guardian, "_launch_state", lambda *_: "loaded" if loaded else "unloaded")
    monkeypatch.setattr(guardian.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(guardian.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def launchctl(argv, **kwargs):
        nonlocal loaded
        if argv[1] == "print":
            assert kwargs == {"capture_output": True, "text": True, "encoding": "utf-8", "timeout": 5}
            job_home = str(tmp_path.resolve())
            if scenario == "foreign-home":
                job_home += "/foreign"
            if scenario == "missing-home":
                job_home = ""
            return subprocess.CompletedProcess(argv, 0, stdout=(
                "environment = {\n"
                f"HERMES_HOME => {job_home}\n"
                f"HERMES_RELEASE_SHA => {'c' * 40 if scenario == 'stale-release' else failed.release_sha}\n"
                f"HERMES_LAUNCHD_LABEL => {'foreign' if scenario == 'wrong-label' else failed.label}\n"
                "}\n"))
        assert argv == ["launchctl", "bootout", f"gui/501/{failed.label}"]
        assert kwargs == {"check": True, "timeout": 15}
        calls.append(argv)
        if scenario == "bootout-error":
            raise subprocess.CalledProcessError(5, argv)
        if scenario != "stubborn":
            loaded = False
        return subprocess.CompletedProcess(argv, 0)

    def rollback(home, failed_id, old_id, epoch, **kwargs):
        assert home == tmp_path and (failed_id, old_id) == (failed.id, predecessor.id)
        assert not loaded, "rollback must not race a loaded KeepAlive job"
        rollbacks.append(epoch)
        return {"from_id": failed_id, "to_id": old_id, "epoch": epoch + 1}

    monkeypatch.setattr(guardian.subprocess, "run", launchctl)
    monkeypatch.setattr(gateway_overlap, "rollback_overlap", rollback)
    result = guardian.run_once(tmp_path, tmp_path / "unused.plist", failed.label)
    refused_identity = scenario in {"foreign-home", "missing-home", "stale-release", "wrong-label",
                                    "stale-boot", "reused-live-pid"}
    if scenario in {"stubborn", "bootout-error"} or refused_identity:
        assert result == "alert" and not rollbacks
        alerts = [json.loads(path.read_text()) for path in (tmp_path / "logs/guardian").glob("*.json")]
        assert any(row["outcome"] == "alert" for row in alerts)
        if scenario == "stubborn":
            assert clock[0] == 15
    else:
        assert result == "rolled_back" and len(rollbacks) == 1
    assert len(calls) == (0 if scenario == "unloaded" or refused_identity else 1)


@pytest.mark.macos_only
@pytest.mark.parametrize("takeover_at", ["lease-read", "launch-state-read"])
def test_guardian_never_bootouts_valid_replacement_of_dead_successor(tmp_path, monkeypatch, takeover_at):
    coordinator, predecessor, failed = _failed_overlap(tmp_path, monkeypatch)
    replacement = GenerationIdentity.create(
        release_sha=failed.release_sha, label=failed.label, pid=predecessor.pid,
        start_fingerprint=predecessor.start_fingerprint)
    original_lease_read = gateway_generation_status.read_active_generation_lease
    loaded = True
    advanced = False
    bootouts = []

    def takeover():
        nonlocal advanced
        if not advanced:
            coordinator.register(replacement, state="serving")
            coordinator.acquire_lease("active_generation", replacement.id)
            advanced = True

    def lease_read(home):
        if takeover_at == "lease-read":
            takeover()
        return original_lease_read(home)

    def launch_state(*_):
        if takeover_at == "launch-state-read":
            takeover()
        return "loaded" if loaded else "unloaded"

    def launchctl(argv, **kwargs):
        nonlocal loaded
        if argv[1] == "print":
            return subprocess.CompletedProcess(argv, 0, stdout=(
                "environment = {\n"
                f"HERMES_HOME => {tmp_path.resolve()}\n"
                f"HERMES_RELEASE_SHA => {failed.release_sha}\n"
                f"HERMES_LAUNCHD_LABEL => {failed.label}\n"
                "}\n"))
        assert argv == ["launchctl", "bootout", f"gui/501/{replacement.label}"]
        # The real coordinator and real status reader prove this is a live owner.
        lease = original_lease_read(tmp_path)
        row = next(row for row in gateway_generation_status.read_generation_status(tmp_path)
                   if row["id"] == replacement.id)
        assert lease is not None
        assert lease["generation_id"] == replacement.id and row["polling_owner"]
        assert row["state"] == "serving" and gateway_overlap._live(row)
        bootouts.append(argv)
        loaded = False
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(gateway_generation_status, "read_active_generation_lease", lease_read)
    monkeypatch.setattr(guardian, "_gateway_domain", lambda *_: "gui/501")
    monkeypatch.setattr(guardian, "_launch_state", launch_state)
    monkeypatch.setattr(guardian.subprocess, "run", launchctl)
    # Use the actual rollback: it refuses the failed generation record, but only
    # after the helper has already booted out the replacement's shared label.
    result = guardian.run_once(tmp_path, tmp_path / "unused.plist", failed.label)
    assert advanced and result == "alert"
    final_lease = original_lease_read(tmp_path)
    assert final_lease is not None and final_lease["generation_id"] == replacement.id
    assert not bootouts, "fencing killed the healthy newer lease holder before rollback rejected stale identity"


@pytest.mark.macos_only
@pytest.mark.parametrize("bootout_fails", [False, True])
def test_external_fence_serializes_writers_and_releases_after_failure(tmp_path, monkeypatch, bootout_fails):
    coordinator, predecessor, failed = _failed_overlap(tmp_path, monkeypatch)
    owner = next(row for row in gateway_generation_status.read_generation_status(tmp_path)
                 if row["id"] == failed.id)
    lease = gateway_generation_status.read_active_generation_lease(tmp_path)
    assert lease is not None
    epoch = lease["epoch"]
    real_run = subprocess.run
    loaded = True
    probes = []
    monkeypatch.setattr(guardian, "_gateway_domain", lambda *_: "gui/501")
    monkeypatch.setattr(guardian, "_launch_state", lambda *_: "loaded" if loaded else "unloaded")

    def launchctl(argv, **kwargs):
        nonlocal loaded
        if argv[1] == "print":
            return subprocess.CompletedProcess(argv, 0, stdout=(
                "environment = {\n"
                f"HERMES_HOME => {tmp_path.resolve()}\n"
                f"HERMES_RELEASE_SHA => {failed.release_sha}\n"
                f"HERMES_LAUNCHD_LABEL => {failed.label}\n"
                "}\n"))
        assert argv == ["launchctl", "bootout", f"gui/501/{failed.label}"]
        # Another OS process reaches the same SQLite writer boundary as
        # register/acquire_lease. It must not commit while bootout is in flight.
        probe = real_run([sys.executable, "-c", (
            "import sqlite3,sys\n"
            "db=sqlite3.connect(sys.argv[1],timeout=0.1,isolation_level=None)\n"
            "try:\n"
            " db.execute('BEGIN IMMEDIATE')\n"
            "except sqlite3.OperationalError as exc:\n"
            " print(str(exc))\n"
            "else:\n"
            " print('writer admitted')\n"
            "finally:\n"
            " db.close()\n"), str(coordinator.path)], capture_output=True, text=True, timeout=5)
        assert probe.returncode == 0 and probe.stdout.strip() == "database is locked"
        probes.append(probe.stdout.strip())
        if bootout_fails:
            raise subprocess.CalledProcessError(5, argv)
        loaded = False
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(guardian.subprocess, "run", launchctl)
    if bootout_fails:
        with pytest.raises(subprocess.CalledProcessError):
            guardian._fence_failed_successor(tmp_path, owner, epoch)
    else:
        guardian._fence_failed_successor(tmp_path, owner, epoch)
    assert probes == ["database is locked"]
    replacement = GenerationIdentity.create(
        release_sha=failed.release_sha, label=failed.label, pid=predecessor.pid,
        start_fingerprint=predecessor.start_fingerprint)
    coordinator.register(replacement, state="serving")
    assert coordinator.acquire_lease("active_generation", replacement.id) > epoch
    final_lease = gateway_generation_status.read_active_generation_lease(tmp_path)
    assert final_lease is not None and final_lease["generation_id"] == replacement.id


@pytest.mark.macos_only
def test_dead_successor_fence_preserves_real_rollback_and_poll_settle(tmp_path, monkeypatch):
    coordinator, predecessor, failed = _failed_overlap(tmp_path, monkeypatch)
    releases = tmp_path / "releases"
    for identity in (predecessor, failed):
        release = releases / identity.release_sha
        release.mkdir(parents=True)
        for marker in (".release-ready", ".hermes_build_sha"):
            (release / marker).write_text(identity.release_sha)
    (tmp_path / "current").symlink_to(releases / failed.release_sha)
    monkeypatch.setattr(gateway_overlap, "_launch_agents_dir", lambda: tmp_path / "LaunchAgents")
    monkeypatch.setattr(guardian, "_gateway_domain", lambda *_: "gui/501")
    loaded = True
    clock = [0.0]
    monkeypatch.setattr(guardian, "_launch_state", lambda *_: "loaded" if loaded else "unloaded")
    monkeypatch.setattr(guardian.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(guardian.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def launchctl(argv, **kwargs):
        nonlocal loaded
        if argv[1] == "print":
            return subprocess.CompletedProcess(argv, 0, stdout=(
                "environment = {\n"
                f"HERMES_HOME => {tmp_path.resolve()}\n"
                f"HERMES_RELEASE_SHA => {failed.release_sha}\n"
                f"HERMES_LAUNCHD_LABEL => {failed.label}\n"
                "}\n"))
        assert argv == ["launchctl", "bootout", f"gui/501/{failed.label}"]
        loaded = False
        return subprocess.CompletedProcess(argv, 0)

    def request(path, verb, *, params, timeout):
        assert verb == "restore_after_rollback" and not loaded
        assert clock[0] == 25, "dead wire must settle before rearming predecessor"
        lease = gateway_generation_status.read_active_generation_lease(tmp_path)
        assert lease is not None and lease["generation_id"] == predecessor.id
        assert params == {"epoch": lease["epoch"]}
        return {"generation_id": predecessor.id, "polling": True}

    monkeypatch.setattr(guardian.subprocess, "run", launchctl)
    monkeypatch.setattr(gateway_overlap, "_generation_request", request)
    assert guardian.run_once(tmp_path, tmp_path / "unused.plist", failed.label) == "rolled_back"
    assert 25 <= clock[0] < 60
    assert (tmp_path / "current").resolve() == releases / predecessor.release_sha
    assert coordinator.leases()[0]["generation_id"] == predecessor.id
