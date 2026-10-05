"""A generation can quiesce polling without tearing down its running turns."""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
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
async def test_transfer_drain_uses_a_clean_context_after_request_deadline(tmp_path):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    adapter = PollingAdapter("clean-context")
    active = ActiveGeneration(tmp_path, db, old, epoch)
    active.bind_runner(Mock(adapters={"telegram": adapter}, _overlap_draining=False,
                            _pending_approvals={}, _active_work_count=lambda: 0))
    db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})
    await active.transfer_requested(new.id, deadline=asyncio.get_running_loop().time() + 0.5)
    assert active._drain_task is not None
    try:
        # Force the pending-transfer watchdog immediately, after the request scope
        # has expired. The watchdog must open its own reserve scope.
        await asyncio.sleep(0.55)
        _, nonce, _ = active._pending_transfer
        active._pending_transfer = (new.id, nonce, time.monotonic() - 1)
        # Re-arm resumes the adapter, then re-checks ownership before it clears
        # the pending transfer; wait for both rather than racing the second await.
        for _ in range(40):
            if adapter.resumed and active._pending_transfer is None:
                break
            await asyncio.sleep(0.05)
        assert adapter.resumed
        assert active._pending_transfer is None
    finally:
        active._drain_task.cancel()
        await asyncio.gather(active._drain_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_split_text_batch_buffered_at_handover_flushes_on_old_owner(tmp_path):
    from gateway.config import Platform
    from gateway.owned_routing import OwnedRouting
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    adapter = PollingAdapter("fake-token")
    adapter.platform = Platform.TELEGRAM
    key = "agent:default:telegram:chat-1"
    event = MessageEvent(text="first chunk second chunk", source=SessionSource(platform=Platform.TELEGRAM, chat_id="1"))
    adapter._pending_text_batches = {key: event}
    adapter._pending_messages = {}
    adapter._active_sessions = {}
    adapter._pending_photo_batches = {}
    adapter._media_group_events = {}
    handled = []
    async def flush(batch_key):
        buffered = adapter._pending_text_batches.pop(batch_key, None)
        if buffered:
            handled.append((old.id, buffered.text))
            adapter._active_sessions[batch_key] = buffered
    adapter._flush_text_batch_now = flush
    runner = Mock(adapters={"telegram": adapter}, _overlap_draining=False, _pending_approvals={})
    active.runner = runner
    active.owned_routing = OwnedRouting(active)
    db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})
    await active.transfer_requested(new.id)
    try:
        assert handled == [(old.id, "first chunk second chunk")]
        assert adapter._pending_text_batches == {}
        assert key in active.owned_routing._live_keys()
        db.commit_transfer(old.id, new.id, epoch)
        with db.connect() as conn:
            assert conn.execute("SELECT generation_id FROM sessions WHERE session_key=?", (key,)).fetchone()[0] == old.id
        # A delayed flush task seeing the emptied buffer cannot deliver twice.
        await adapter._flush_text_batch_now(key)
        assert handled == [(old.id, "first chunk second chunk")]
    finally:
        assert active._drain_task is not None
        active._drain_task.cancel()
        await asyncio.gather(active._drain_task, return_exceptions=True)


@pytest.mark.parametrize("buffer_name, flush_name, text", [
    ("_pending_photo_batches", "_flush_photo_batch_now", "photo"),
    ("_media_group_events", "_flush_media_group_now", "album"),
])
@pytest.mark.asyncio
async def test_media_buffered_at_handover_flushes_on_old_owner(tmp_path, buffer_name, flush_name, text):
    from gateway.config import Platform
    from gateway.owned_routing import OwnedRouting
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    adapter = PollingAdapter("fake-token")
    adapter.platform = Platform.TELEGRAM
    key = "agent:default:telegram:chat-1"
    event = MessageEvent(text=text, source=SessionSource(platform=Platform.TELEGRAM, chat_id="1"))
    setattr(adapter, buffer_name, {key: event})
    adapter._pending_text_batches = {}
    adapter._pending_messages = {}
    adapter._active_sessions = {}
    handled = []
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from types import MethodType
    task_name = "_pending_photo_batch_tasks" if buffer_name == "_pending_photo_batches" else "_media_group_tasks"
    setattr(adapter, task_name, {})
    flush = getattr(TelegramAdapter, flush_name, None)
    if flush is not None:
        setattr(adapter, flush_name, MethodType(flush, adapter))
    async def handle(buffered):
        assert db.leases()[0]["generation_id"] == old.id
        handled.append((old.id, buffered.text))
        adapter._active_sessions[key] = buffered
    adapter.handle_message = handle
    runner = Mock(adapters={"telegram": adapter}, _overlap_draining=False, _pending_approvals={})
    active.runner = runner
    active.owned_routing = OwnedRouting(active)
    db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})
    await active.transfer_requested(new.id)
    try:
        assert handled == [(old.id, text)]
        assert getattr(adapter, buffer_name) == {}
        db.commit_transfer(old.id, new.id, epoch)
        await getattr(adapter, flush_name)(key)
        assert handled == [(old.id, text)]
    finally:
        assert active._drain_task is not None
        active._drain_task.cancel()
        await asyncio.gather(active._drain_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_old_runner_retains_work_after_polling_stops(tmp_path):
    db = GenerationCoordinator(tmp_path)
    fingerprint = f"{os.getpid()}:{_get_process_start_time(os.getpid())}"
    old = GenerationIdentity.create(release_sha="a", label="a", start_fingerprint=fingerprint)
    new = GenerationIdentity.create(release_sha="b", label="b", start_fingerprint=fingerprint)
    db.register(old, state="serving")
    db.register(new, state="standby")
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
async def test_abandoned_transfer_rearms_old_generation_and_allows_retry(tmp_path, monkeypatch):
    import gateway.run_generation as generation_run

    monkeypatch.setattr(generation_run, "HANDOVER_REQUEST_TIMEOUT", .2, raising=False)
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    adapter = PollingAdapter("fake-token")
    runner = Mock(adapters={"telegram": adapter}, _overlap_draining=False)
    active.bind_runner(runner)
    db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})
    await active.transfer_requested(new.id)
    try:
        deadline = asyncio.get_running_loop().time() + 3
        # The wire resumes before the final asynchronous owner check reopens dispatch.
        while (not adapter.resumed or runner._overlap_draining) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(.05)
        assert adapter.resumed
        assert runner._overlap_draining is False
        with db.connect() as conn:
            row = conn.execute("SELECT state FROM generation_transfers WHERE old_id=?", (old.id,)).fetchone()
        assert row["state"] == "aborted"
        db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})
    finally:
        active._drain_task.cancel()
        await asyncio.gather(active._drain_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_driver_aborts_without_ack_and_old_generation_rearms(tmp_path, monkeypatch):
    import gateway.run_generation as generation_run

    monkeypatch.setattr(generation_run, "HANDOVER_REQUEST_TIMEOUT", .2)
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    adapter = PollingAdapter("fake-token")
    runner = Mock(adapters={"telegram": adapter}, _overlap_draining=False)
    active.bind_runner(runner)
    db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})
    await active.transfer_requested(new.id)
    assert active._pending_transfer is not None
    nonce = active._pending_transfer[1]
    assert db.abort_transfer(old.id, new.id, epoch, attempt_nonce=nonce)
    assert active._drain_task is not None
    drain_task = active._drain_task
    try:
        await asyncio.wait_for(drain_task, 3)
        assert adapter.resumed
        assert active._pending_transfer is None
        assert runner._overlap_draining is False
    finally:
        if not drain_task.done():
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_committed_transfer_cannot_be_aborted_by_old_deadline(tmp_path, monkeypatch):
    import gateway.run_generation as generation_run

    monkeypatch.setattr(generation_run, "HANDOVER_REQUEST_TIMEOUT", .2, raising=False)
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    adapter = PollingAdapter("fake-token")
    runner = Mock(adapters={"telegram": adapter}, _overlap_draining=False,
                  _active_work_count=lambda: 1, _pending_approvals={})
    active.bind_runner(runner)
    db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})
    await active.transfer_requested(new.id)
    try:
        db.commit_transfer(old.id, new.id, epoch)
        await asyncio.sleep(1.3)
        assert not adapter.resumed
        assert runner._overlap_draining is True
        assert db.leases()[0]["generation_id"] == new.id
        with db.connect() as conn:
            row = conn.execute("SELECT state FROM generation_transfers WHERE old_id=?", (old.id,)).fetchone()
        assert row["state"] == "committed"
    finally:
        active._drain_task.cancel()
        await asyncio.gather(active._drain_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_transfer_nonce_lookup_does_not_block_event_loop(tmp_path, monkeypatch):
    import threading

    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    active.bind_runner(Mock(adapters={}, _overlap_draining=False))
    db.request_transfer(old.id, new.id, epoch, set())
    original = db.transfer_attempt_nonce
    entered, release = threading.Event(), threading.Event()

    def slow_nonce(*args):
        entered.set()
        release.wait(3)
        return original(*args)

    monkeypatch.setattr(db, "transfer_attempt_nonce", slow_nonce)
    task = asyncio.create_task(active.transfer_requested(new.id))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not task.done()  # The nonce read did not freeze the event loop.
    finally:
        release.set()
        await task
        assert active._drain_task is not None
        active._drain_task.cancel()
        await asyncio.gather(active._drain_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_old_generation_waits_for_real_work_then_exits(tmp_path):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
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
    db.register(new, state="standby")
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
    db.register(new, state="standby")
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
async def test_registry_process_and_pending_notice_each_wait_until_deadline(tmp_path, monkeypatch):
    import gateway.run_generation as generation_run
    from tools.process_registry import process_registry
    db = GenerationCoordinator(tmp_path)
    old, new = GenerationIdentity.create(release_sha="a", label="a"), GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    db.request_transfer(old.id, new.id, epoch, set())
    db.commit_transfer(old.id, new.id, epoch)
    now = [1000.0]
    monkeypatch.setattr(generation_run.time, "time", lambda: now[0])
    with db.connect() as conn:
        conn.execute("UPDATE generations SET drain_deadline=? WHERE id=?", (now[0] + 5, old.id))
    for held_by_process in (True, False):
        active = ActiveGeneration(tmp_path, db, old, epoch)
        stopped = []
        async def stop():
            stopped.append(True)
        active.runner = Mock(_overlap_draining=True, _active_work_count=lambda: 0,
                             _pending_approvals={}, stop=stop)
        monkeypatch.setattr(process_registry, "has_any_active", lambda: held_by_process)
        monkeypatch.setattr(process_registry, "pending_watchers", [] if held_by_process else [{"type": "complete"}])
        now[0] = 1000.0
        assert not await active.finish_draining_once()
        assert stopped == []
        now[0] = 1005.0
        assert await active.finish_draining_once()
        assert stopped == [True]


@pytest.mark.asyncio
async def test_deadline_exit_releases_outstanding_claim_for_successor(tmp_path):
    import time
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
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
    db.register(new, state="standby")
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
    assert await active.finish_draining_once()
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
async def test_polling_stop_failure_keeps_old_lease(tmp_path):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
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
async def test_partial_rearm_failure_keeps_dispatch_fenced_and_recovers_other_pollers(tmp_path, caplog):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    first, second, third = (PollingAdapter(name) for name in ("first", "second", "third"))

    async def failed_rearm(receipt):
        raise RuntimeError("first poller cannot restart")

    async def failed_stop():
        raise RuntimeError("original stop failure")

    first.start_polling_from_transfer = failed_rearm
    third.stop_polling_for_transfer = failed_stop
    runner = Mock(adapters={"one": first, "two": second, "three": third}, _overlap_draining=False)
    active.bind_runner(runner)
    db.request_transfer(old.id, new.id, epoch, {
        adapter._controlled_journal.token_hash for adapter in (first, second, third)})
    with pytest.raises(RuntimeError, match="re-arm failed") as exc:
        await active.transfer_requested(new.id)
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert "original stop failure" in str(exc.value.__cause__)
    assert second.resumed
    assert runner._overlap_draining is True
    assert first._controlled_journal.token_hash in caplog.text
    assert "original stop failure" in caplog.text
    assert db.leases()[0]["generation_id"] == old.id
    active._sync_runtime_status()
    from gateway.status import read_runtime_status
    health = read_runtime_status(active.paths["state"])
    assert health["needs_attention"] is True and health["polling"] is False
    assert first._controlled_journal.token_hash in health["error_message"]
    from hermes_cli.gateway_generation_status import read_generation_status
    old_status = next(row for row in read_generation_status(tmp_path) if row["id"] == old.id)
    assert old_status["needs_attention"] is True and old_status["polling"] is False




@pytest.mark.asyncio
async def test_transfer_abort_failure_surfaces_attention_status(tmp_path, monkeypatch):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    adapter = PollingAdapter("fake-token")

    async def fail_stop():
        raise RuntimeError("original stop failure")

    adapter.stop_polling_for_transfer = fail_stop
    def abort_fails(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "abort_transfer", abort_fails)
    runner = Mock(adapters={"telegram": adapter}, _overlap_draining=False)
    active.bind_runner(runner)
    db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})

    with pytest.raises(RuntimeError, match="abort could not be proved"):
        await active.transfer_requested(new.id)

    from gateway.status import read_runtime_status
    status = read_runtime_status(active.paths["state"])
    assert status["needs_attention"] is True
    assert status["polling"] is False
    assert "abort could not be proved" in status["error_message"]
    assert runner._overlap_draining is True




@pytest.mark.asyncio
async def test_draining_coordinator_io_does_not_block_event_loop(tmp_path, monkeypatch):
    import threading

    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    db.request_transfer(old.id, new.id, epoch, set())
    db.commit_transfer(old.id, new.id, epoch)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    stopped = []

    async def stop():
        stopped.append(True)

    runner = Mock(adapters={}, _overlap_draining=True, _pending_approvals={},
                  _active_work_count=lambda: 0, stop=stop)
    active.bind_runner(runner)
    # Isolate the drain inspection from the concurrently running admission loop.
    assert active.owned_routing is not None and active.owned_routing._task is not None
    active.owned_routing._task.cancel()
    original_generations = db.generations
    original_connect = db.connect
    generations_entered, generations_release = threading.Event(), threading.Event()
    connect_entered, connect_release = threading.Event(), threading.Event()

    def slow_generations():
        generations_entered.set()
        generations_release.wait(3)
        return original_generations()

    def slow_connect():
        connect_entered.set()
        connect_release.wait(3)
        return original_connect()

    monkeypatch.setattr(db, "generations", slow_generations)
    monkeypatch.setattr(db, "connect", slow_connect)
    task = asyncio.create_task(active.finish_draining_once())
    assert await asyncio.to_thread(generations_entered.wait, 2)
    tick = asyncio.Event()
    asyncio.get_running_loop().call_soon(tick.set)
    await asyncio.wait_for(tick.wait(), 1)
    assert not task.done()

    generations_release.set()
    assert await asyncio.to_thread(connect_entered.wait, 2)
    tick = asyncio.Event()
    asyncio.get_running_loop().call_soon(tick.set)
    await asyncio.wait_for(tick.wait(), 1)
    assert not task.done()
    connect_release.set()
    assert await task is True
    assert stopped == [True]


@pytest.mark.asyncio
async def test_transfer_fences_cron_and_goal_before_poller_stops(tmp_path, monkeypatch):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
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
async def test_concurrent_drain_inspections_stop_runner_once(tmp_path, monkeypatch):
    import threading

    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    db.request_transfer(old.id, new.id, epoch, set())
    db.commit_transfer(old.id, new.id, epoch)
    with db.connect() as conn:
        conn.execute("UPDATE generations SET drain_deadline=0 WHERE id=?", (old.id,))
    active = ActiveGeneration(tmp_path, db, old, epoch)
    stopped = []

    async def stop():
        stopped.append(True)

    active.bind_runner(Mock(adapters={}, _overlap_draining=True, _pending_approvals={},
                            _active_work_count=lambda: 1, stop=stop))
    entered, release = threading.Event(), threading.Event()
    original_fence = db.fence_draining_generation

    def slow_fence(*args):
        entered.set()
        release.wait(2)
        return original_fence(*args)

    monkeypatch.setattr(db, "fence_draining_generation", slow_fence)
    first = asyncio.create_task(active.finish_draining_once())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(active.finish_draining_once())
        await asyncio.sleep(.1)
    finally:
        release.set()
    assert await first is True
    assert await second in (False, True)
    assert stopped == [True]


@pytest.mark.asyncio
async def test_missing_drain_deadline_uses_local_cap_without_repeated_warning(tmp_path, caplog, monkeypatch):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="a")
    new = GenerationIdentity.create(release_sha="b", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    stopped = []

    async def stop():
        stopped.append(True)

    runner = Mock(adapters={}, _pending_approvals={}, _active_work_count=lambda: 1, stop=stop)
    active.bind_runner(runner)
    db.request_transfer(old.id, new.id, epoch, set())
    await active.transfer_requested(new.id)
    # Isolate the repeated inspection from the production drain task.
    assert active._drain_task is not None
    active._drain_task.cancel()
    await asyncio.gather(active._drain_task, return_exceptions=True)
    db.commit_transfer(old.id, new.id, epoch)
    with db.connect() as conn:
        conn.execute("UPDATE generations SET drain_deadline=NULL WHERE id=?", (old.id,))
    import gateway.run_generation as generation_run
    now = [10_000.0]
    monkeypatch.setattr(generation_run.time, "time", lambda: now[0])
    assert not await active.finish_draining_once()
    now[0] += 7199
    assert not await active.finish_draining_once()
    assert stopped == []
    now[0] += 2
    assert await active.finish_draining_once()
    assert await active.finish_draining_once()
    assert stopped == [True]
    assert len([r for r in caplog.records if "missing drain deadline" in r.message]) == 1


@pytest.mark.asyncio
async def test_takeover_releases_failed_claim_before_retry(monkeypatch):
    import gateway.run_generation as generation_run
    import gateway.status as status

    held = False
    attempts = 0
    removed = []

    def claim():
        nonlocal held, attempts
        attempts += 1
        held = True
        if attempts == 1:
            raise SystemExit(75)
        return True

    def release():
        nonlocal held
        held = False

    async def start_socket():
        return "claimed"

    real_sleep = asyncio.sleep

    async def sleep(delay):
        assert delay <= 5
        await real_sleep(0)

    monkeypatch.setattr(status, "get_running_pid", lambda: None)
    monkeypatch.setattr(status, "is_gateway_runtime_lock_active", lambda: held)
    monkeypatch.setattr(status, "owns_gateway_runtime_lock", lambda: held, raising=False)
    monkeypatch.setattr(status, "remove_pid_file", lambda: removed.append(True))
    monkeypatch.setattr(status, "release_gateway_runtime_lock", release)
    monkeypatch.setattr(generation_run.asyncio, "sleep", sleep)
    identity = GenerationIdentity.create(release_sha="b", label="b")
    assert await asyncio.wait_for(generation_run.take_over_legacy_gateway_resources(
        identity, claim=claim, start_socket=start_socket, refresh=lambda: None), 2) == "claimed"
    assert attempts == 2 and removed == [True]


@pytest.mark.asyncio
async def test_claim_retry_registers_exit_cleanup_once(monkeypatch):
    import atexit
    import gateway.run as gateway_run
    import gateway.status as status

    registrations = []
    monkeypatch.setattr(atexit, "register", lambda fn: registrations.append(fn))
    monkeypatch.setattr(status, "acquire_gateway_runtime_lock", lambda: True)
    monkeypatch.setattr(status, "get_running_pid", lambda: None)
    monkeypatch.setattr(status, "write_pid_file", lambda **kwargs: None)
    monkeypatch.setattr(gateway_run, "_claim_host_gateway_role", lambda **kwargs: None)
    monkeypatch.setattr(gateway_run, "_pid_cleanup_registered", False, raising=False)
    assert gateway_run._start_gateway_claim_pid_file()
    assert gateway_run._start_gateway_claim_pid_file()
    assert registrations == [status.remove_pid_file, status.release_gateway_runtime_lock]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [False, True], ids=["before-pause", "materialized-during-flush"])
async def test_invalid_live_key_retains_polling_and_cron_after_transfer_abort(tmp_path, late):
    import threading
    from types import SimpleNamespace
    from gateway.config import Platform
    from gateway.owned_routing import OwnedRouting

    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="old", label="old")
    new = GenerationIdentity.create(release_sha="new", label="new")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    adapter = PollingAdapter("fake-token")
    adapter.platform = Platform.TELEGRAM
    adapter._active_sessions = {} if late else {"unscoped": object()}
    adapter._pending_messages = {}
    adapter._pending_text_batches = {"agent:default:telegram:pending": object()} if late else {}
    adapter._pending_photo_batches = {}
    adapter._media_group_events = {}

    async def flush(key):
        adapter._pending_text_batches.pop(key)
        adapter._active_sessions["unscoped"] = object()

    adapter._flush_text_batch_now = flush
    provider = Mock()
    active.cron_provider = provider
    active.cron_stop = threading.Event()
    runner = SimpleNamespace(adapters={"telegram": adapter}, _pending_approvals={},
                             _overlap_draining=False, _overlap_cron_start_kwargs={})
    active.runner = runner
    active.owned_routing = OwnedRouting(active)
    db.request_transfer(old.id, new.id, epoch, {adapter._controlled_journal.token_hash})
    with pytest.raises(RuntimeError, match="unscoped session obligation"):
        await active.transfer_requested(new.id)
    assert adapter.stopped == late
    assert adapter.resumed == late
    assert runner._overlap_draining is False
    assert not active.cron_stop.is_set()
    provider.stop.assert_not_called()
    provider.start.assert_not_called()
    assert not active._external_cron_stopped
    assert db.leases()[0]["generation_id"] == old.id
    assert active._drain_task is None


@pytest.fixture(autouse=True)
def _coordinator_boot_identity(monkeypatch):
    # Unit transactions use a stable supplied boot identity. Native process
    # and launchd suites continue to probe the actual host.
    monkeypatch.setattr("gateway.generation._boot_id", lambda: "unit-test-boot")


def test_cold_activation_honours_an_expired_enclosing_deadline(tmp_path):
    from gateway import deadline as gd
    from gateway.run_generation import _activate_cold_generation
    db = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha="a", label="a")
    db.register(identity, state="standby")
    with gd.deadline_scope(gd.now() - 1):
        with pytest.raises(TimeoutError):
            _activate_cold_generation(db, identity)
    assert not [row for row in db.leases() if row["resource"] == "active_generation"
                and row["generation_id"] == identity.id and row["state"] == "active"]


def test_cold_activation_without_scope_gets_a_concrete_bound(tmp_path, monkeypatch):
    from gateway import deadline as gd
    from gateway import run_generation
    db = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha="a", label="a")
    db.register(identity, state="standby")
    seen = {}
    acquire = db.acquire_lease
    def spy(*args, **kwargs):
        seen["deadline"] = kwargs.get("deadline")
        seen["scope"] = gd.current()
        return acquire(*args, **kwargs)
    monkeypatch.setattr(db, "acquire_lease", spy)
    assert gd.current() is None
    run_generation._activate_cold_generation(db, identity)
    assert seen["deadline"] is not None and seen["scope"] == seen["deadline"]
    assert seen["deadline"] <= gd.now() + run_generation.COLD_ACTIVATION_SECONDS
