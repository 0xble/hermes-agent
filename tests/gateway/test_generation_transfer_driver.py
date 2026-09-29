"""Transfer driver must refuse promotion until the old process has stopped polling."""
from __future__ import annotations

import asyncio
import hashlib
import os

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
    db.register(new, state="ready")
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
    db.register(new, state="ready")
    epoch = db.acquire_lease("active_generation", old.id)
    with pytest.raises(RuntimeError, match="control"):
        await asyncio.to_thread(handover_to_generation, tmp_path, new.id, timeout=.2)
    assert db.leases()[0]["generation_id"] == old.id
    assert db.leases()[0]["epoch"] == epoch
