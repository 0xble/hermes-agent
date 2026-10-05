"""Transfer driver must refuse promotion until the old process has stopped polling."""
from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import time

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity
from gateway.run_generation import ActiveGeneration, handover_to_generation
from gateway.status import _get_process_start_time


class _Adapter:
    def __init__(self):
        self._controlled_journal = type("Journal", (), {"token_hash": hashlib.sha256(b"test").hexdigest()})()
        self.stopped = False

    async def stop_polling_for_transfer(self):
        self.stopped = True
        return {"token_hash": self._controlled_journal.token_hash, "safe_offset": 7, "epoch": 1}

    async def start_polling_from_transfer(self, receipt):
        self.stopped = False


@pytest.mark.asyncio
async def test_driver_uses_old_control_socket_before_committing(tmp_path):
    db = GenerationCoordinator(tmp_path)
    fp = f"{os.getpid()}:{_get_process_start_time(os.getpid())}"
    old = GenerationIdentity.create(release_sha="a", label="a", start_fingerprint=fp)
    new = GenerationIdentity.create(release_sha="b", label="b", start_fingerprint=fp)
    db.register(old, state="serving")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    adapter = _Adapter()
    active.bind_runner(type("Runner", (), {"adapters": {"telegram": adapter}})())
    await active.start()
    db.register(new, state="standby")
    successor = ActiveGeneration(tmp_path, db, new, epoch + 1)
    successor_adapter = _Adapter()
    successor_adapter._controlled_poller = type("Poller", (), {"running": True})()
    successor_adapter._polling_progress_event = asyncio.Event()
    successor_adapter._polling_progress_event.set()
    successor.bind_runner(type("Runner", (), {"adapters": {"telegram": successor_adapter}})())
    await successor.start()
    try:
        result = await asyncio.to_thread(handover_to_generation, tmp_path, new.id, timeout=4)
        assert result == epoch + 1
        assert adapter.stopped
        assert db.leases()[0]["generation_id"] == new.id
    finally:
        await active.close()
        await successor.close()


@pytest.mark.asyncio
async def test_driver_fails_closed_if_old_process_cannot_acknowledge(tmp_path):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    with pytest.raises(RuntimeError, match="control"):
        await asyncio.to_thread(handover_to_generation, tmp_path, new.id, timeout=.2)
    assert db.leases()[0]["generation_id"] == old.id
    assert db.leases()[0]["epoch"] == epoch


@pytest.mark.asyncio
async def test_failed_commit_rearms_old_polling_and_dispatch(tmp_path, monkeypatch):
    db = GenerationCoordinator(tmp_path)
    fp = f"{os.getpid()}:{_get_process_start_time(os.getpid())}"
    old = GenerationIdentity.create(release_sha="a", label="a", start_fingerprint=fp)
    new = GenerationIdentity.create(release_sha="b", label="b", start_fingerprint=fp)
    db.register(old, state="serving")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    adapter = _Adapter()
    runner = type("Runner", (), {"adapters": {"telegram": adapter}, "_overlap_draining": False})()
    active.bind_runner(runner)
    await active.start()
    db.register(new, state="standby")
    def fail_commit(*args, **kwargs):
        raise RuntimeError("injected commit failure")
    monkeypatch.setattr(GenerationCoordinator, "commit_transfer", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="injected commit failure"):
            await asyncio.to_thread(handover_to_generation, tmp_path, new.id, timeout=4)
        assert db.leases()[0]["generation_id"] == old.id
        assert not adapter.stopped
        assert not runner._overlap_draining
        assert not await active.finish_draining_once()
    finally:
        await active.close()


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_hide_original_transfer_failure(tmp_path, monkeypatch):
    from gateway import run_generation
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    db.acquire_lease("active_generation", old.id)
    monkeypatch.setattr(run_generation, "_generation_request", lambda *a, **kw: {"tokens": []}
                        if a[1] == "polling_roster" else (_ for _ in ()).throw(RuntimeError("original stop failure")))
    monkeypatch.setattr(GenerationCoordinator, "abort_transfer",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("cleanup failure")))
    with pytest.raises(RuntimeError, match="original stop failure"):
        await asyncio.to_thread(handover_to_generation, tmp_path, new.id)


@pytest.mark.asyncio
async def test_committed_but_unverified_handover_has_typed_outcome(tmp_path):
    from gateway import run_generation
    db = GenerationCoordinator(tmp_path)
    fp = f"{os.getpid()}:{_get_process_start_time(os.getpid())}"
    old = GenerationIdentity.create(release_sha="a", label="a", start_fingerprint=fp)
    new = GenerationIdentity.create(release_sha="b", label="b", start_fingerprint=fp)
    db.register(old, state="serving")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    active.bind_runner(type("Runner", (), {"adapters": {}})())
    await active.start()
    db.register(new, state="standby")
    try:
        with pytest.raises(run_generation.HandoverCommittedUnverified) as exc:
            await asyncio.to_thread(handover_to_generation, tmp_path, new.id, timeout=.3)
        assert exc.value.epoch == epoch + 1
        assert exc.value.generation_id == new.id
        assert db.leases()[0]["generation_id"] == new.id
    finally:
        await active.close()


@pytest.mark.asyncio
async def test_late_commit_returns_committed_epoch_not_plain_failure(tmp_path, monkeypatch):
    """A commit that lands after the deadline already moved the lease.

    Raising a plain error here would make the updater treat a healthy committed
    successor as a pre-commit failure and roll it back.
    """
    import time as _time
    db = GenerationCoordinator(tmp_path)
    fp = f"{os.getpid()}:{_get_process_start_time(os.getpid())}"
    old = GenerationIdentity.create(release_sha="a", label="a", start_fingerprint=fp)
    new = GenerationIdentity.create(release_sha="b", label="b", start_fingerprint=fp)
    db.register(old, state="serving")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    active.bind_runner(type("Runner", (), {"adapters": {}})())
    await active.start()
    db.register(new, state="standby")
    original = GenerationCoordinator.commit_transfer

    def late_commit(self, *args, **kwargs):
        promoted = original(self, *args, **kwargs)
        _time.sleep(1.2)  # commit completes, then the deadline passes
        return promoted

    monkeypatch.setattr(GenerationCoordinator, "commit_transfer", late_commit)
    try:
        promoted = await asyncio.to_thread(handover_to_generation, tmp_path, new.id,
                                           timeout=1.0, verify_after_commit=False)
        assert promoted == epoch + 1
        assert db.leases()[0]["generation_id"] == new.id
    finally:
        await active.close()


@pytest.mark.asyncio
async def test_commit_transfer_does_not_move_lease_after_busy_deadline(tmp_path):
    """A writer lock acquired after the handover window cannot move the lease."""
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    db.request_transfer(old.id, new.id, epoch, set())

    lock = sqlite3.connect(db.path, timeout=5.0, isolation_level=None)
    try:
        lock.execute("BEGIN IMMEDIATE")
        deadline = time.monotonic() + 0.15
        with pytest.raises(TimeoutError, match="deadline"):
            await asyncio.to_thread(
                db.commit_transfer, old.id, new.id, epoch, deadline=deadline
            )
    finally:
        lock.rollback()
        lock.close()

    lease = db.leases()[0]
    assert (lease["generation_id"], lease["epoch"]) == (old.id, epoch)


@pytest.mark.asyncio
async def test_takeover_dead_generation_does_not_retire_after_busy_deadline(tmp_path):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    lock = sqlite3.connect(db.path, timeout=5.0, isolation_level=None)
    booted = []
    try:
        lock.execute("BEGIN IMMEDIATE")
        deadline = time.monotonic() + 0.15
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="deadline"):
            await asyncio.to_thread(
                db.takeover_dead_generation, "active_generation", old.id, new.id,
                bootout=lambda label: booted.append(label) or True,
                death_proof=lambda row: True, deadline=deadline,
            )
        assert time.monotonic() - started < 1.0
    finally:
        lock.rollback()
        lock.close()
    assert not booted
    assert db.leases()[0]["generation_id"] == old.id
    retired = next(row for row in db.generations() if row["id"] == old.id)
    assert retired["verdict"] is None and retired["state"] == "serving"


@pytest.mark.asyncio
async def test_abort_transfer_does_not_change_state_after_busy_deadline(tmp_path):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    db.request_transfer(old.id, new.id, epoch, set())
    nonce = db.transfer_attempt_nonce(old.id, epoch)
    lock = sqlite3.connect(db.path, timeout=5.0, isolation_level=None)
    try:
        lock.execute("BEGIN IMMEDIATE")
        with pytest.raises(TimeoutError, match="deadline"):
            await asyncio.to_thread(
                db.abort_transfer, old.id, new.id, epoch,
                attempt_nonce=nonce, deadline=time.monotonic() + 0.15,
            )
    finally:
        lock.rollback()
        lock.close()
    assert db.leases()[0]["generation_id"] == old.id
    assert db.transfer_receipts(old.id, epoch) == []
    with db.connect() as conn:
        assert conn.execute(
            "SELECT state FROM generation_transfers WHERE old_id=? AND epoch=?",
            (old.id, epoch)).fetchone()["state"] == "requested"


@pytest.mark.asyncio
async def test_record_poller_stopped_does_not_persist_after_busy_deadline(tmp_path):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    db.request_transfer(old.id, new.id, epoch, {"token"})
    nonce = db.transfer_attempt_nonce(old.id, epoch)
    lock = sqlite3.connect(db.path, timeout=5.0, isolation_level=None)
    try:
        lock.execute("BEGIN IMMEDIATE")
        with pytest.raises(TimeoutError, match="deadline"):
            await asyncio.to_thread(
                db.record_poller_stopped, old.id, epoch, "token", 7,
                attempt_nonce=nonce, deadline=time.monotonic() + 0.15,
            )
    finally:
        lock.rollback()
        lock.close()
    assert db.transfer_receipts(old.id, epoch)[0]["poller_stopped"] == 0


@pytest.fixture(autouse=True)
def _coordinator_boot_identity(monkeypatch):
    # Unit transactions use a stable supplied boot identity. Native process
    # and launchd suites continue to probe the actual host.
    monkeypatch.setattr("gateway.generation._boot_id", lambda: "unit-test-boot")
