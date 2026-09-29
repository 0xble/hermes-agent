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


@pytest.mark.parametrize("rollback_blocks", [False, True])
def test_promotion_returns_rolled_back_proof_after_committed_observation_failure(tmp_path, monkeypatch, rollback_blocks):
    from hermes_cli import gateway_overlap
    home = tmp_path / "profile"
    home.mkdir()
    coordinator, first, second, epoch = _generations(home)
    paths = home / "releases"
    for identity in (first, second):
        release = paths / identity.release_sha
        release.mkdir(parents=True)
        for marker in (".release-ready", ".hermes_build_sha"):
            (release / marker).write_text(identity.release_sha)
    (home / "current").symlink_to(paths / first.release_sha)
    from dataclasses import asdict
    monkeypatch.setattr(gateway_overlap, "_active_and_prior", lambda _: (coordinator, asdict(first), None, epoch))
    monkeypatch.setattr(guardian, "_launch_state", lambda *args: "unloaded")
    monkeypatch.setattr(guardian, "_gateway_domain", lambda *args: "gui/501")
    monkeypatch.setattr(gateway_overlap, "render_generation_launchd_plist", lambda **kwargs: "test")
    monkeypatch.setattr(gateway_overlap, "bootstrap_generation_plist", lambda **kwargs: None)
    monkeypatch.setattr(gateway_overlap, "_ready_successor", lambda *args, **kwargs: asdict(second))
    def activate(_, release, **kwargs):
        pointer = home / "current"
        pointer.unlink()
        pointer.symlink_to(release)
    monkeypatch.setattr(gateway_overlap, "activate_release", activate)
    def commit_then_fail(*args, **kwargs):
        coordinator.request_transfer(first.id, second.id, epoch, set())
        coordinator.commit_transfer(first.id, second.id, epoch)
        raise RuntimeError("committed but unverified")
    monkeypatch.setattr(gateway_overlap, "handover_to_generation", commit_then_fail)
    def rollback(*args, **kwargs):
        if rollback_blocks:
            raise RuntimeError("successor wire-stop unavailable")
        restored = coordinator.rollback_transfer(second.id, first.id, epoch + 1, poller_stopped=True)
        activate(home, paths / first.release_sha)
        return {"epoch": restored, "to_id": first.id}
    monkeypatch.setattr(gateway_overlap, "rollback_overlap", rollback)
    if rollback_blocks:
        with pytest.raises(RuntimeError, match="overlap blocked.*wire-stop unavailable"):
            gateway_overlap.promote_overlap(home, paths / second.release_sha, second.release_sha)
        assert coordinator.leases()[0]["generation_id"] == second.id
    else:
        result = gateway_overlap.promote_overlap(home, paths / second.release_sha, second.release_sha)
        assert result["outcome"] == "rolled_back" and result["rollback"]["to_id"] == first.id
        assert coordinator.leases()[0]["generation_id"] == first.id


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


def test_rollback_uses_selected_drain_cap_and_records_transfer_time(tmp_path):
    coordinator, first, second, epoch = _generations(tmp_path)
    coordinator.request_transfer(first.id, second.id, epoch, set())
    promoted = coordinator.commit_transfer(first.id, second.id, epoch, drain_seconds=90)
    before = next(row for row in coordinator.generations() if row["id"] == first.id)
    assert before["drain_deadline"] - before["transferred_at"] == pytest.approx(90)
    coordinator.rollback_transfer(second.id, first.id, promoted, poller_stopped=True, drain_seconds=45)
    after = next(row for row in coordinator.generations() if row["id"] == second.id)
    assert 40 < after["drain_deadline"] - time.time() <= 45


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


def test_guardian_observes_real_status_identity_before_socket_failure(tmp_path):
    from hermes_cli.gateway_overlap import _observe_poller
    coordinator, first, second, epoch = _generations(tmp_path)
    row = next(row for row in read_generation_status(tmp_path) if row["id"] == first.id)
    with pytest.raises(RuntimeError, match="generation control unavailable"):
        _observe_poller(tmp_path, row, timeout=.1)


def test_guardian_keeps_healthy_successor_polling(tmp_path, monkeypatch):
    import socket
    import threading
    from gateway.generation import generation_paths
    home = tmp_path / "profile"
    home.mkdir()
    coordinator, first, second, epoch = _generations(home)
    coordinator.request_transfer(first.id, second.id, epoch, set())
    coordinator.commit_transfer(first.id, second.id, epoch)
    with coordinator.connect() as conn:
        conn.execute("UPDATE generations SET pid=?,start_fingerprint=? WHERE id=?",
                     (first.pid, first.start_fingerprint, second.id))
        conn.execute("UPDATE generations SET transferred_at=? WHERE id=?",
                     (time.time() - 40, first.id))
    path = generation_paths(home, second)["socket"]
    path.parent.mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(1)
    def respond():
        connection, _ = server.accept()
        with connection:
            connection.recv(4096)
            connection.sendall((json.dumps({"ok": True, "result": {
                "generation_id": second.id, "polling": True, "tokens": ["token"]}}) + "\n").encode())
    worker = threading.Thread(target=respond, daemon=True)
    worker.start()
    try:
        assert guardian._run_overlap(home) == "healthy"
    finally:
        server.close()
        worker.join(timeout=2)
    assert coordinator.leases()[0]["generation_id"] == second.id


@pytest.mark.asyncio
async def test_refused_rollback_rearms_successor_from_stopped_receipts(tmp_path):
    from gateway.run_generation import ActiveGeneration
    coordinator, first, second, epoch = _generations(tmp_path)
    coordinator.request_transfer(first.id, second.id, epoch, {"token"})
    coordinator.record_poller_stopped(first.id, epoch, "token", 0)
    promoted = coordinator.commit_transfer(first.id, second.id, epoch)
    class Adapter:
        def __init__(self):
            self._controlled_journal = type("Journal", (), {"token_hash": "token"})()
            self.started = []
        async def stop_polling_for_transfer(self):
            return {"token_hash": "token", "safe_offset": 0}
        async def start_polling_from_transfer(self, receipt):
            self.started.append(receipt)
    adapter = Adapter()
    active = ActiveGeneration(tmp_path, coordinator, second, promoted)
    active.runner = type("Runner", (), {"adapters": {"telegram": adapter}, "_overlap_draining": False})()
    stopped = await active.stop_for_rollback()
    assert stopped["poller_stopped"] is True
    assert coordinator.rollback_transfer(second.id, first.id, promoted + 1, poller_stopped=True) is None
    armed = await active.resume_uncommitted_transfer(promoted)
    assert armed["polling"] is True
    assert adapter.started == [{"token_hash": "token", "safe_offset": 0}]
    assert active._drain_task is None


@pytest.mark.asyncio
async def test_consumed_transfer_rearms_by_fresh_connect_on_owned_generation(tmp_path):
    from gateway.run_generation import ActiveGeneration
    coordinator, first, second, epoch = _generations(tmp_path)
    from plugins.platforms.telegram.polling_transfer import PollingJournal
    journal = PollingJournal(coordinator, "123456:DISPOSABLE_TEST")
    receipt = journal.stop_receipt()
    events = []

    class Adapter:
        _controlled_journal = journal

        async def start_polling_from_transfer(self, receipt):
            events.append(("transfer", receipt["epoch"]))
            journal.begin_successor(receipt)
            raise OSError("cold start failed after begin_successor")

        async def disconnect(self):
            events.append(("disconnect",))

        async def connect(self):
            lease = coordinator.leases()[0]
            events.append(("connect", lease["generation_id"], lease["epoch"]))
            return True

    active = ActiveGeneration(tmp_path, coordinator, first, epoch)
    await active._rearm_adapter(Adapter(), receipt)
    assert journal.validate_transfer(receipt) is False
    assert events == [("transfer", receipt["epoch"]), ("disconnect",), ("connect", first.id, epoch)]


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
        conn.execute("UPDATE generations SET transferred_at=? WHERE id=?", (time.time() - 40, first.id))
    from hermes_cli import gateway_overlap
    monkeypatch.setattr(guardian, "_repair_count", lambda home: 0)
    monkeypatch.setattr(guardian, "_gateway_domain", lambda label, preferred: "gui/501")
    monkeypatch.setattr(guardian, "_launch_state", lambda domain, label: "unloaded")
    monkeypatch.setattr(gateway_overlap, "_observe_poller", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no progress")))
    calls = []
    monkeypatch.setattr(gateway_overlap, "rollback_overlap", lambda *args, **kwargs: calls.append(args) or {"epoch": next_epoch + 1})
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
