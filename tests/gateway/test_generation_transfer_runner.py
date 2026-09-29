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
async def test_unscoped_background_process_and_watcher_hold_generation_until_delivery(tmp_path, monkeypatch):
    from tools.process_registry import process_registry
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="ready")
    epoch = db.acquire_lease("active_generation", old.id)
    db.request_transfer(old.id, new.id, epoch, set())
    db.commit_transfer(old.id, new.id, epoch)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    stopped = []
    async def stop():
        stopped.append(True)
    active.runner = Mock(_overlap_draining=True, _active_work_count=lambda: 0,
                         _pending_approvals={}, stop=stop)
    process = Mock(session_key="", exited=False)
    monkeypatch.setattr(process_registry, "_running", {"unscoped": process})
    monkeypatch.setattr(process_registry, "_refresh_detached_session", lambda s: s)
    monkeypatch.setattr(process_registry, "pending_watchers", [{"session_key": "", "type": "complete"}])
    assert not await active.finish_draining_once()
    assert stopped == []
    process.exited = True
    assert not await active.finish_draining_once()
    # The notification is handed to the gateway before it can stop.
    delivered = process_registry.pending_watchers.pop()
    assert delivered["type"] == "complete"
    assert await active.finish_draining_once()
    assert stopped == [True]


@pytest.mark.asyncio
async def test_deadline_exit_releases_outstanding_claim_for_successor(tmp_path):
    import time
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="ready")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    async def stop():
        return None
    active.runner = Mock(_overlap_draining=True, _active_work_count=lambda: 0,
                         _pending_approvals={}, stop=stop)
    db.claim_session(str(tmp_path), "telegram", "chat", old.id, epoch, outstanding_work=1)
    db.request_transfer(old.id, new.id, epoch, set())
    new_epoch = db.commit_transfer(old.id, new.id, epoch, drain_seconds=1)
    with db.connect() as conn, conn:
        conn.execute("UPDATE generations SET drain_deadline=? WHERE id=?", (time.time() - 1, old.id))
    assert await active.finish_draining_once()
    await active.close()
    with db.connect() as conn:
        claim = conn.execute("SELECT generation_id,epoch,outstanding_work FROM sessions WHERE session_key='chat'").fetchone()
    assert tuple(claim) == (new.id, new_epoch, 0)


@pytest.mark.asyncio
async def test_cap_fences_queued_work_before_stopping_busy_runner(tmp_path):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="ready")
    epoch = db.acquire_lease("active_generation", old.id)
    db.claim_session(str(tmp_path), "telegram", "agent:main:busy", old.id, epoch, outstanding_work=1)
    db.enqueue(str(tmp_path), "telegram", "agent:main:busy", "queued", "message",
               b'{"version":1,"authorized":true,"sender":"1"}', b'queued', old.id, epoch)
    db.request_transfer(old.id, new.id, epoch, set())
    active = ActiveGeneration(tmp_path, db, old, epoch)
    stopped = []
    async def stop():
        with db.connect() as conn:
            stopped.append(dict(conn.execute("SELECT state,outstanding_work FROM sessions WHERE generation_id=?", (old.id,)).fetchone()))
    runner = Mock(adapters={}, _running_agents={"agent:main:busy": object()},
                  _pending_approvals={"agent:main:busy": object()},
                  _active_work_count=lambda: 1, stop=stop)
    active.bind_runner(runner)
    await active.transfer_requested(new.id)
    db.commit_transfer(old.id, new.id, epoch, drain_seconds=1)
    with db.connect() as conn:
        conn.execute("UPDATE generations SET drain_deadline=? WHERE id=?", (0, old.id))
    await asyncio.gather(active.finish_draining_once(), active.finish_draining_once())
    if active._drain_task is not None:
        await asyncio.wait_for(active._drain_task, timeout=5)
    assert len(stopped) == 1
    assert stopped == [{"state": "interrupted", "outstanding_work": 1}]
    with db.connect() as conn:
        receipt = conn.execute("SELECT state,payload,owner_id FROM inbox WHERE source_event_id='queued'").fetchone()
        assert (receipt["state"], receipt["payload"], receipt["owner_id"]) == ("interrupted", b"queued", old.id)
    assert runner._overlap_cap_interrupted is True
    await active.close()
    with db.connect() as conn:
        claim = conn.execute("SELECT generation_id,state FROM sessions WHERE session_key='agent:main:busy'").fetchone()
        assert tuple(claim) == (new.id, "interrupted")
    row, fresh = db.enqueue(str(tmp_path), "telegram", "agent:main:busy", "late", "message",
                            b'{"version":1,"authorized":true,"sender":"1"}', b'late', new.id, epoch + 1)
    assert fresh and row["owner_id"] == new.id
    from gateway.run_shutdown import GatewayShutdownMixin
    runner._restart_requested = False
    runner.async_session_store = Mock()
    assert await GatewayShutdownMixin._mark_running_sessions_resume_pending(runner, "cap") == []
    runner.async_session_store.mark_resume_pending.assert_not_called()


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
