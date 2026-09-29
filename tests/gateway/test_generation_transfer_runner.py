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
async def test_session_keyed_process_without_claim_keeps_draining_owner_until_notice(tmp_path, monkeypatch):
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
    process = Mock(session_key="agent:default:cron:ended", exited=False)
    monkeypatch.setattr(process_registry, "_running", {"session-bound": process})
    monkeypatch.setattr(process_registry, "_refresh_detached_session", lambda s: s)
    monkeypatch.setattr(process_registry, "pending_watchers", [])
    assert not await active.finish_draining_once()
    assert stopped == []
    process.exited = True
    notice = {"session_key": process.session_key, "type": "complete"}
    process_registry.pending_watchers.append(notice)
    assert not await active.finish_draining_once()
    assert process_registry.pending_watchers.pop() is notice
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
    # This test owns the cap call; stop the concurrently scheduled drain loop.
    assert active._drain_task is not None
    active._drain_task.cancel()
    await asyncio.gather(active._drain_task, return_exceptions=True)
    db.commit_transfer(old.id, new.id, epoch, drain_seconds=1)
    with db.connect() as conn:
        conn.execute("UPDATE generations SET drain_deadline=? WHERE id=?", (0, old.id))
    await asyncio.gather(active.finish_draining_once(), active.finish_draining_once())
    assert active._drain_task is not None and active._drain_task.cancelled()
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
    runner = Mock(adapters={"telegram": adapter}, _overlap_draining=False)
    active.bind_runner(runner)
    db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})
    with pytest.raises(RuntimeError, match="poll not stopped"):
        await active.transfer_requested(new.id)
    assert db.leases()[0]["generation_id"] == old.id
    assert not db.transfer_receipts(old.id, epoch)[0]["poller_stopped"]
    assert runner._overlap_draining is False


@pytest.mark.asyncio
async def test_transfer_fences_cron_and_goal_before_poller_stops(tmp_path, monkeypatch):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="ready")
    epoch = db.acquire_lease("active_generation", old.id)
    adapter = PollingAdapter("fake-token")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_stop():
        entered.set()
        await release.wait()
        return {"token_hash": adapter._controlled_journal.token_hash, "safe_offset": 23}

    adapter.stop_polling_for_transfer = slow_stop
    runner = Mock(adapters={"telegram": adapter}, _overlap_draining=False)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    active.bind_runner(runner)
    db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})
    transfer = asyncio.create_task(active.transfer_requested(new.id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert runner._overlap_draining is True

        from cron.scheduler_provider import InProcessCronScheduler
        import cron.scheduler as scheduler
        import cron.jobs as jobs
        from gateway.run import GatewayRunner

        calls = []
        monkeypatch.setattr(scheduler, "tick", lambda **kwargs: calls.append("cron"))
        monkeypatch.setattr(jobs, "record_ticker_heartbeat", lambda **kwargs: None)
        monkeypatch.setattr(InProcessCronScheduler, "recover_interrupted", lambda self: 0)
        stop_event = Mock()
        stop_event.is_set.side_effect = [False, True]
        stop_event.wait.return_value = True
        InProcessCronScheduler().start(
            stop_event, interval=0, can_dispatch=lambda: not runner._overlap_draining)
        assert calls == []

        runner._running = True
        with monkeypatch.context() as context:
            async def one_scan_sleep(delay):
                runner._running = False
            context.setattr(asyncio, "sleep", one_scan_sleep)
            await GatewayRunner._loop_wakeup_watcher(runner, interval=0)
        assert calls == []
        runner._warm_goals_session_db.assert_not_called()  # No goal wake scan began.
    finally:
        release.set()
        await transfer


@pytest.mark.asyncio
async def test_serving_successor_is_not_demoted_by_ready_mark(tmp_path):
    db = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha="b", label="b")
    db.register(identity, state="serving")
    active = ActiveGeneration(tmp_path, db, identity, 1)
    await active.mark_ready()
    assert next(row for row in db.generations() if row["id"] == identity.id)["state"] == "serving"


@pytest.mark.asyncio
async def test_missing_drain_deadline_expires_without_repeated_failure(tmp_path, caplog):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="ready")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    stopped = []

    async def stop():
        stopped.append(True)

    runner = Mock(adapters={}, _pending_approvals={}, _active_work_count=lambda: 1, stop=stop)
    active.bind_runner(runner)
    db.request_transfer(old.id, new.id, epoch, set())
    await active.transfer_requested(new.id)
    db.commit_transfer(old.id, new.id, epoch)
    with db.connect() as conn:
        conn.execute("UPDATE generations SET drain_deadline=NULL WHERE id=?", (old.id,))
    assert await active.finish_draining_once()
    assert not await active.finish_draining_once()
    assert stopped == [True]
    assert len([r for r in caplog.records if "missing drain deadline" in r.message]) == 1


@pytest.mark.asyncio
async def test_legacy_takeover_backs_off_and_warns_once(tmp_path, monkeypatch, caplog):
    import gateway.run_generation as generation_run
    import gateway.status as status

    identity = GenerationIdentity.create(release_sha="b", label="b")
    elapsed = 0.0
    attempts = 0
    intervals = []

    def claim():
        nonlocal attempts
        attempts += 1
        return False

    async def sleep(seconds):
        nonlocal elapsed
        intervals.append(seconds)
        elapsed += seconds
        if elapsed >= 70:
            raise StopAfterProbe()

    class StopAfterProbe(Exception):
        pass

    monkeypatch.setattr(status, "get_running_pid", lambda: os.getpid())
    monkeypatch.setattr(status, "is_gateway_runtime_lock_active", lambda: False)
    monkeypatch.setattr(generation_run.asyncio, "sleep", sleep)
    with pytest.raises(StopAfterProbe):
        await generation_run.take_over_legacy_gateway_resources(
            identity, claim=claim, start_socket=lambda: None, refresh=lambda: None)
    assert attempts <= 8, (attempts, intervals)
    assert intervals[0] >= 1 and max(intervals) >= 30, intervals
    assert len([r for r in caplog.records if "retrying slowly" in r.message]) == 1
