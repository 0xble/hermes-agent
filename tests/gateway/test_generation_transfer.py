"""Cooperative promotion contracts over a real coordinator database."""
from __future__ import annotations

import os

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity
from gateway.status import _get_process_start_time


def _pair(tmp_path):
    coordinator = GenerationCoordinator(tmp_path)
    fingerprint = f"{os.getpid()}:{_get_process_start_time(os.getpid())}"
    old = GenerationIdentity.create(release_sha="a", label="slot-a", start_fingerprint=fingerprint)
    new = GenerationIdentity.create(release_sha="b", label="slot-b", start_fingerprint=fingerprint)
    coordinator.register(old, state="serving")
    coordinator.register(new, state="ready")
    epoch = coordinator.acquire_lease("active_generation", old.id)
    return coordinator, old, new, epoch


def test_transfer_requires_every_served_token_receipt_and_ready_successor(tmp_path):
    db, old, new, epoch = _pair(tmp_path)
    tokens = {"first", "second"}
    db.request_transfer(old.id, new.id, epoch, tokens)
    db.record_poller_stopped(old.id, epoch, "first", 17)
    with pytest.raises(RuntimeError, match="receipt"):
        db.commit_transfer(old.id, new.id, epoch)
    assert db.leases()[0]["generation_id"] == old.id
    db.heartbeat(new.id, state="standby")
    db.record_poller_stopped(old.id, epoch, "second", 21)
    with pytest.raises(RuntimeError, match="ready"):
        db.commit_transfer(old.id, new.id, epoch)
    db.heartbeat(new.id, state="ready")
    promoted = db.commit_transfer(old.id, new.id, epoch)
    assert promoted == epoch + 1
    assert db.leases()[0]["generation_id"] == new.id
    assert next(row for row in db.generations() if row["id"] == old.id)["state"] == "draining"
    assert {r["token_hash"]: r["safe_offset"] for r in db.transfer_receipts(old.id, epoch)} == {
        "first": 17, "second": 21}
    with pytest.raises(RuntimeError):
        db.commit_transfer(old.id, new.id, epoch)
    assert db.leases()[0]["epoch"] == promoted


def test_transfer_rejects_forged_receipts_and_stale_epoch(tmp_path):
    db, old, new, epoch = _pair(tmp_path)
    db.request_transfer(old.id, new.id, epoch, {"first"})
    with pytest.raises(RuntimeError):
        db.record_poller_stopped(new.id, epoch, "first", 3)
    with pytest.raises(RuntimeError):
        db.record_poller_stopped(old.id, epoch + 1, "first", 3)
    with pytest.raises(RuntimeError):
        db.record_poller_stopped(old.id, epoch, "other", 3)
    with pytest.raises(RuntimeError):
        db.commit_transfer(old.id, new.id, epoch)
    assert db.leases()[0]["generation_id"] == old.id


def test_transfer_cannot_steal_live_holder_without_request(tmp_path):
    db, old, new, epoch = _pair(tmp_path)
    with pytest.raises(RuntimeError):
        db.commit_transfer(old.id, new.id, epoch)
    with pytest.raises(RuntimeError):
        db.acquire_lease("active_generation", new.id)
    assert db.leases()[0]["generation_id"] == old.id
