"""Non-live launchd fencing contracts and takeover regression for PR #266.

The strict xfails expose destructive stale-label fencing, not a safety approval.
Run with --runxfail to reproduce the violated no-bootout invariant.
"""
from __future__ import annotations

import json
import os
import subprocess

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
@pytest.mark.parametrize("scenario", ["loaded", "unloaded", "stubborn", "bootout-error"])
def test_failed_successor_fence_requires_unloaded_label_before_rollback(tmp_path, monkeypatch, scenario):
    _coordinator, predecessor, failed = _failed_overlap(tmp_path, monkeypatch)
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
    if scenario in {"stubborn", "bootout-error"}:
        assert result == "alert" and not rollbacks
        alerts = [json.loads(path.read_text()) for path in (tmp_path / "logs/guardian").glob("*.json")]
        assert any(row["outcome"] == "alert" for row in alerts)
        if scenario == "stubborn":
            assert clock[0] == 15
    else:
        assert result == "rolled_back" and len(rollbacks) == 1
    assert len(calls) == (0 if scenario == "unloaded" else 1)


@pytest.mark.macos_only
@pytest.mark.parametrize("takeover_at", ["lease-read", "launch-state-read"])
@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="PR #266 fences stale label after a valid successor acquires a newer lease")
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
