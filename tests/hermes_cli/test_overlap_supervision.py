"""Behavioral checks for opt-in generation supervision and status."""
from __future__ import annotations

import json
import plistlib
import time
from pathlib import Path

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity
from hermes_cli import gateway_guardian as guardian
from hermes_cli.gateway_generation_status import read_generation_status


def _generations(home: Path):
    coordinator = GenerationCoordinator(home)
    from gateway.status import _get_process_start_time
    import os
    pid = os.getpid()
    first = GenerationIdentity.create(release_sha="a" * 40, label="ai.hermes.gateway", pid=pid,
                                      start_fingerprint=f"{pid}:{_get_process_start_time(pid)}")
    second = GenerationIdentity.create(release_sha="b" * 40, label="ai.hermes.gateway-b", pid=456)
    coordinator.register(first, state="serving")
    coordinator.register(second, state="ready")
    epoch = coordinator.acquire_lease("active_generation", first.id)
    return coordinator, first, second, epoch


def test_rollback_requires_stopped_successor_and_increments_epoch(tmp_path):
    coordinator, first, second, epoch = _generations(tmp_path)
    coordinator.request_transfer(first.id, second.id, epoch, set())
    successor_epoch = coordinator.commit_transfer(first.id, second.id, epoch)
    with pytest.raises(RuntimeError, match="poller"):
        coordinator.rollback_transfer(second.id, first.id, successor_epoch, poller_stopped=False)
    assert coordinator.leases()[0]["generation_id"] == second.id
    restored_epoch = coordinator.rollback_transfer(second.id, first.id, successor_epoch, poller_stopped=True)
    assert restored_epoch > successor_epoch
    assert coordinator.leases()[0]["generation_id"] == first.id
    assert coordinator.rollback_transfer(second.id, first.id, successor_epoch, poller_stopped=True) is None


def test_status_identifies_polling_owner_and_draining_obligations(tmp_path):
    coordinator, first, second, epoch = _generations(tmp_path)
    coordinator.request_transfer(first.id, second.id, epoch, set())
    coordinator.commit_transfer(first.id, second.id, epoch)
    with coordinator.connect() as conn:
        conn.execute("INSERT INTO sessions(profile_home,transport,session_key,generation_id,epoch,state,outstanding_work) "
                     "VALUES(?,?,?,?,?,'active',?)", (str(tmp_path), "telegram", "chat:1", first.id, epoch, 2))
    rows = {row["id"]: row for row in read_generation_status(tmp_path)}
    assert rows[second.id]["polling_owner"] is True
    assert rows[first.id]["polling_owner"] is False
    assert rows[first.id]["draining_count"] == 2
    assert rows[second.id]["draining_count"] == 0


def test_drain_cap_fences_only_unfinished_sessions_once(tmp_path):
    coordinator, first, second, epoch = _generations(tmp_path)
    coordinator.request_transfer(first.id, second.id, epoch, set())
    coordinator.commit_transfer(first.id, second.id, epoch, drain_seconds=1)
    with coordinator.connect() as conn:
        conn.execute("INSERT INTO sessions(profile_home,transport,session_key,generation_id,epoch,state,outstanding_work) "
                     "VALUES(?,?,?,?,?,'owned',?)", (str(tmp_path), "telegram", "chat:1", first.id, epoch, 1))
        conn.execute("UPDATE generations SET drain_deadline=? WHERE id=?", (time.time() - 1, first.id))
    assert coordinator.interrupt_at_drain_cap(first.id) == 1
    assert coordinator.interrupt_at_drain_cap(first.id) == 0
    with coordinator.connect() as conn:
        session = conn.execute("SELECT state,outstanding_work FROM sessions WHERE generation_id=?", (first.id,)).fetchone()
        evidence = conn.execute("SELECT side_effect_evidence FROM generation_interruptions "
                                "WHERE generation_id=?", (first.id,)).fetchall()
    assert tuple(session) == ("interrupted", 1)
    assert len(evidence) == 1 and "unknown" in evidence[0][0]


def test_guardian_failed_poller_uses_fenced_rollback_within_health_window(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("gateway:\n  overlap_handover:\n    enabled: true\n")
    coordinator, first, second, epoch = _generations(home)
    coordinator.request_transfer(first.id, second.id, epoch, set())
    next_epoch = coordinator.commit_transfer(first.id, second.id, epoch)
    with coordinator.connect() as conn:
        conn.execute("UPDATE generations SET drain_deadline=? WHERE id=?", (time.time() + 7200 - 40, first.id))
    from hermes_cli import gateway_overlap
    monkeypatch.setattr(guardian, "_repair_count", lambda home: 0)
    monkeypatch.setattr(guardian, "_gateway_domain", lambda label, preferred: "gui/501")
    monkeypatch.setattr(guardian, "_launch_state", lambda domain, label: "unloaded")
    monkeypatch.setattr(gateway_overlap, "_observe_poller", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no progress")))
    calls = []
    monkeypatch.setattr(gateway_overlap, "rollback_overlap", lambda *args: calls.append(args) or {"epoch": next_epoch + 1})
    # Both identities are known, but no launchctl mutation is authorized until
    # the guarded helper has proved the successor wire stopped.
    with coordinator.connect() as conn:
        conn.execute("UPDATE generations SET pid=?,start_fingerprint=? WHERE id=?",
                     (first.pid, first.start_fingerprint, second.id))
    assert guardian._run_overlap(home) == "rolled_back"
    assert calls and calls[0][1:] == (second.id, first.id, next_epoch)


@pytest.mark.macos_only
def test_guardian_alerts_on_unknown_active_identity_without_touching_launchd(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("gateway:\n  overlap_handover:\n    enabled: true\n")
    coordinator, first, second, epoch = _generations(home)
    plist = home / "legacy.plist"
    plist.write_bytes(plistlib.dumps({"Label": first.label, "EnvironmentVariables": {"HERMES_HOME": str(home)}}))
    monkeypatch.setattr("gateway.status._get_process_start_time", lambda pid: None)
    calls = []
    monkeypatch.setattr(guardian.subprocess, "run", lambda *args, **kwargs: calls.append(args) or pytest.fail("launchctl mutation"))
    assert guardian.run_once(home, plist, first.label) == "alert"
    assert not calls
    outcomes = [json.loads(path.read_text()) for path in (home / "logs/guardian").glob("*.json")]
    assert any(row["outcome"] == "alert" for row in outcomes)
