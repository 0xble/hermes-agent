"""A generation can quiesce polling without tearing down its running turns."""
from __future__ import annotations

import asyncio
import hashlib
import os
from unittest.mock import Mock

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity
from gateway.run_generation import ActiveGeneration
from gateway.status import _get_process_start_time


class PollingAdapter:
    def __init__(self, token):
        self.config = Mock(token=token)
        self._controlled_journal = Mock(token_hash=hashlib.sha256(token.encode()).hexdigest())
        self.stopped = False
        self.resumed = False

    async def stop_polling_for_transfer(self):
        self.stopped = True
        return {"token_hash": self._controlled_journal.token_hash, "epoch": 1, "safe_offset": 23}

    async def start_polling_from_transfer(self, receipt):
        self.resumed = True


@pytest.mark.asyncio
async def test_old_runner_retains_work_after_polling_stops(tmp_path):
    db = GenerationCoordinator(tmp_path)
    fingerprint = f"{os.getpid()}:{_get_process_start_time(os.getpid())}"
    old = GenerationIdentity.create(release_sha="a", label="a", start_fingerprint=fingerprint)
    new = GenerationIdentity.create(release_sha="b", label="b", start_fingerprint=fingerprint)
    db.register(old, state="serving")
    db.register(new, state="ready")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    adapter = PollingAdapter("fake-token")
    live_turn = object()
    runner = Mock(adapters={"telegram": adapter}, _running_agents={"busy": live_turn})
    active.bind_runner(runner)
    db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})
    answer = await active.transfer_requested(new.id)
    assert answer["poller_stopped"]
    assert db.transfer_receipts(old.id, epoch)[0]["safe_offset"] == 23
    assert runner._running_agents["busy"] is live_turn
    assert adapter.stopped
    assert not adapter.resumed


@pytest.mark.asyncio
async def test_old_generation_waits_for_real_work_then_exits(tmp_path):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="ready")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    work = {"turn": object()}
    stopped = []
    async def stop():
        stopped.append(True)
    runner = Mock(adapters={}, _running_agents=work, _pending_approvals={},
                  _active_work_count=lambda: len(work), stop=stop)
    active.bind_runner(runner)
    db.request_transfer(old.id, new.id, epoch, set())
    await active.transfer_requested(new.id)
    db.commit_transfer(old.id, new.id, epoch)
    assert not await active.finish_draining_once()
    assert not stopped
    work.clear()
    assert await active.finish_draining_once()
    assert stopped == [True]

@pytest.mark.asyncio
async def test_aborted_transfer_disconnects_adapter_that_fails_to_rearm(tmp_path):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="ready")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    adapter = PollingAdapter("fake-token")
    disconnected = []
    async def start(_receipt):
        raise RuntimeError("poll may still be running")
    async def disconnect():
        disconnected.append(True)
    adapter.start_polling_from_transfer = start
    adapter.disconnect = disconnect
    active.bind_runner(Mock(adapters={"telegram": adapter}))
    db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})
    await active.transfer_requested(new.id)
    db.abort_transfer(old.id, new.id, epoch)
    with pytest.raises(RuntimeError, match="poll may still be running"):
        await active.transfer_aborted(new.id)
    assert disconnected == [True]


@pytest.mark.asyncio
async def test_polling_stop_failure_keeps_old_lease(tmp_path):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="ready")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    adapter = PollingAdapter("fake-token")
    async def fail():
        raise RuntimeError("poll not stopped")
    adapter.stop_polling_for_transfer = fail
    active.bind_runner(Mock(adapters={"telegram": adapter}))
    db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})
    with pytest.raises(RuntimeError, match="poll not stopped"):
        await active.transfer_requested(new.id)
    assert db.leases()[0]["generation_id"] == old.id
    assert not db.transfer_receipts(old.id, epoch)[0]["poller_stopped"]
