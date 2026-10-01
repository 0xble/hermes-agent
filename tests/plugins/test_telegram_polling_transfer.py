"""Durable Telegram polling boundary and transfer invariants."""
import asyncio
import json
import threading

import pytest
pytest.importorskip("telegram")
from telegram import Update

from gateway.generation import GenerationCoordinator
from plugins.platforms.telegram.polling_transfer import PollingJournal, ControlledPoller


@pytest.mark.asyncio
async def test_wire_journal_commits_before_offset_and_replays_unaccepted(tmp_path):
    coordinator = GenerationCoordinator(tmp_path)
    journal = PollingJournal(coordinator, "123456:LOCAL_ONLY")
    batch = [{"update_id": 10, "message": {"text": "a"}}, {"update_id": 11, "message": {"text": "b"}}]
    journal.record_response(json.dumps({"ok": True, "result": batch}).encode())
    assert journal.safe_offset() == 12
    assert [item["update_id"] for item in journal.pending()] == [10, 11]
    assert await journal.claim(10)
    assert not await journal.claim(10)
    assert [item["update_id"] for item in journal.pending()] == [11]
    await journal.accept(10)
    journal.record_response(json.dumps({"ok": True, "result": batch}).encode())
    assert [item["update_id"] for item in journal.pending()] == [11]
    assert not await journal.claim(10)


@pytest.mark.asyncio
async def test_idle_reset_allows_nonmonotonic_ids_and_prunes_accepted_rows(tmp_path, monkeypatch):
    from plugins.platforms.telegram import polling_transfer

    clock = [1_000_000.0]
    monkeypatch.setattr(polling_transfer.time, "time", lambda: clock[0])
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    journal.record_response(b'{"ok":true,"result":[{"update_id":900000}]}')
    assert await journal.claim(900000)
    await journal.accept(900000)
    clock[0] += 8 * 24 * 60 * 60
    assert journal.safe_offset() == 0
    journal.record_response(b'{"ok":true,"result":[{"update_id":5}]}')
    assert journal.safe_offset() == 6
    assert [row["update_id"] for row in journal.pending()] == [5]
    with journal._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM telegram_updates").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_retention_runs_on_empty_poll_response(tmp_path, monkeypatch):
    from plugins.platforms.telegram import polling_transfer

    clock = [1_000_000.0]
    monkeypatch.setattr(polling_transfer.time, "time", lambda: clock[0])
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    journal.record_response(b'{"ok":true,"result":[{"update_id":5}]}')
    assert await journal.claim(5)
    await journal.accept(5)
    clock[0] += 2 * 24 * 60 * 60
    journal.record_response(b'{"ok":true,"result":[]}')
    with journal._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM telegram_updates").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_retention_prunes_terminal_rows_but_preserves_received(tmp_path, monkeypatch):
    from plugins.platforms.telegram import polling_transfer
    clock = [1_000_000.0]
    monkeypatch.setattr(polling_transfer.time, "time", lambda: clock[0])
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    journal.record_response(b'{"ok":true,"result":[{"update_id":1},{"update_id":2},{"update_id":3}]}')
    assert await journal.claim(1)  # Ambiguous effect, never replay.
    with journal._connect() as db:
        db.execute("UPDATE telegram_updates SET state='quarantined' WHERE update_id=2")
    clock[0] += 2 * 24 * 60 * 60
    journal.record_response(b'{"ok":true,"result":[]}')
    with journal._connect() as db:
        assert [(r["update_id"], r["state"]) for r in db.execute(
            "SELECT update_id,state FROM telegram_updates ORDER BY update_id")] == [(3, "received")]


def test_transfer_receipt_fences_epoch_and_bot_identity(tmp_path):
    coordinator = GenerationCoordinator(tmp_path)
    first = PollingJournal(coordinator, "123456:LOCAL_ONLY")
    other = PollingJournal(coordinator, "789:OTHER_LOCAL_ONLY")
    first.record_response(b'{"ok":true,"result":[{"update_id":41}]}')
    receipt = first.stop_receipt()
    assert receipt["safe_offset"] == 42
    assert other.token_hash != receipt["token_hash"]
    assert first.validate_transfer(receipt)
    assert not other.validate_transfer(receipt)
    assert not first.validate_transfer({**receipt, "epoch": receipt["epoch"] + 1})


@pytest.mark.asyncio
async def test_join_queue_returns_when_polling_is_stopped(tmp_path):
    from types import SimpleNamespace
    queue = asyncio.Queue()
    await queue.put(object())
    poller = ControlledPoller(SimpleNamespace(update_queue=queue),
                              PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY"))
    poller._stop.set()
    await asyncio.wait_for(poller._join_queue("stopped batch"), timeout=1)
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_controlled_poller_replays_before_first_poll_and_drains_inflight(tmp_path):
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    journal.record_response(b'{"ok":true,"result":[{"update_id":4}]}')
    request_started = asyncio.Event()
    release_request = asyncio.Event()
    queued = []

    class Bot:
        async def get_updates(self, *, offset, timeout, allowed_updates):
            assert offset == 5
            assert queued == [4]
            request_started.set()
            await release_request.wait()
            return []

    class Queue:
        async def put(self, update):
            queued.append(update.update_id)
            assert await journal.claim(update.update_id)
            await journal.accept(update.update_id)

        async def join(self):
            pass

    class App:
        bot = Bot()
        update_queue = Queue()

    poller = ControlledPoller(App(), journal, timeout=20)
    await poller.start()
    await asyncio.wait_for(request_started.wait(), 2)
    stop = asyncio.create_task(poller.stop())
    await asyncio.sleep(0.02)
    assert not stop.done()  # A cancelled task is not evidence the wire has closed.
    release_request.set()
    assert (await asyncio.wait_for(stop, 3))["stopped"]
    assert not poller.running


@pytest.mark.asyncio
async def test_failed_claim_reopens_without_replaying_ambiguous_processing(tmp_path):
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    journal.record_response(b'{"ok":true,"result":[{"update_id":17}]}')
    assert await journal.claim(17)
    assert journal.pending() == []  # A crash after handoff might have external effects.
    await journal.reopen(17)
    assert [item["update_id"] for item in journal.pending()] == [17]
    assert await journal.claim(17)
    await journal.accept(17)
    assert journal.pending() == []


@pytest.mark.asyncio
async def test_processing_row_does_not_pin_idle_reset(tmp_path, monkeypatch):
    from plugins.platforms.telegram import polling_transfer
    clock = [1_000_000.0]
    monkeypatch.setattr(polling_transfer.time, "time", lambda: clock[0])
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    journal.record_response(b'{"ok":true,"result":[{"update_id":900000}]}')
    assert await journal.claim(900000)
    clock[0] += 8 * 24 * 60 * 60
    assert journal.safe_offset() == 0
    journal.record_response(b'{"ok":true,"result":[{"update_id":5}]}')
    assert journal.safe_offset() == 6
    assert [row["update_id"] for row in journal.pending()] == [5]


def test_receipt_validation_and_repr_never_expose_token(tmp_path):
    token = "123456:LOCAL_ONLY"
    journal = PollingJournal(GenerationCoordinator(tmp_path), token)
    assert token not in repr(journal)
    receipt = journal.stop_receipt()
    for invalid in (None, "42", True):
        with pytest.raises(RuntimeError, match="safe_offset"):
            journal.validate_transfer({**receipt, "safe_offset": invalid})


def test_poison_update_is_quarantined_without_blocking_valid_ones(tmp_path, monkeypatch):
    from plugins.platforms.telegram import polling_transfer
    monkeypatch.setattr(polling_transfer, "_MAX_RAW_UPDATE", 80)
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    journal.record_response(json.dumps({"ok": True, "result": [
        {"update_id": 9, "message": {"text": "x" * 100}},
        {"update_id": "invalid"}, {"update_id": 10},
    ]}).encode())
    assert journal.safe_offset() == 11
    assert [row["update_id"] for row in journal.pending()] == [10]
    with journal._connect() as db:
        assert db.execute("SELECT state FROM telegram_updates WHERE update_id=9").fetchone()[0] == "quarantined"


@pytest.mark.asyncio
async def test_journal_io_runs_off_loop_and_queue_join_backpressures(tmp_path):
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    journal.record_response(b'{"ok":true,"result":[{"update_id":4}]}')
    main_thread = threading.get_ident()
    worker_threads = []
    original = journal._connect

    def observed():
        worker_threads.append(threading.get_ident())
        return original()

    journal._connect = observed
    assert await journal.claim(4)
    await journal.accept(4)
    assert worker_threads and all(ident != main_thread for ident in worker_threads)

    # One short queue-join deadline must not count as a failed poll.
    release_join = asyncio.Event()
    class Queue:
        async def put(self, update):
            pass
        async def join(self):
            await release_join.wait()
    stop_request = asyncio.Event()
    class Bot:
        calls = 0
        async def get_updates(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return [Update(update_id=5)]
            await stop_request.wait()
            return []
    class App:
        bot = Bot()
        update_queue = Queue()
    poller = ControlledPoller(App(), journal, timeout=0.02)
    journal.record_response(b'{"ok":true,"result":[{"update_id":5}]}')
    worker_threads.clear()  # Test fixture write above is synchronous, unlike poller I/O.
    start = asyncio.create_task(poller.start())
    await asyncio.sleep(0.08)
    assert not start.done()
    release_join.set()
    await asyncio.wait_for(start, 1)
    deadline = asyncio.get_running_loop().time() + 2
    while App.bot.calls < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert App.bot.calls >= 2  # safe_offset before and after the returned batch
    assert worker_threads and all(ident != main_thread for ident in worker_threads)
    stop_request.set()
    assert (await asyncio.wait_for(poller.stop(), 1))["stopped"]


@pytest.mark.asyncio
async def test_stop_waits_for_queued_dispatch_and_times_out_if_unfinished(tmp_path):
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    class Bot:
        async def get_updates(self, **kwargs):
            await asyncio.Event().wait()
    class App:
        bot = Bot()
        update_queue = asyncio.Queue()
    poller = ControlledPoller(App(), journal, timeout=.05)
    # A completed wire request may coexist with an update already handed to PTB.
    poller._task = asyncio.create_task(asyncio.sleep(0))
    await poller._task
    await App.update_queue.put(Update(update_id=4))
    stop = asyncio.create_task(poller.stop())
    await asyncio.sleep(.02)
    assert not stop.done()
    assert (await stop) == {"stopped": False, "error": "PollDrainTimeout"}
    update = App.update_queue.get_nowait()
    assert update.update_id == 4
    App.update_queue.task_done()
    assert await poller.stop() == {"stopped": True}


@pytest.mark.asyncio
async def test_failed_poller_exception_observed_once_without_disconnect(tmp_path, caplog):
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    poller = ControlledPoller(object(), journal)
    async def exhausted():
        raise OSError("all retries exhausted")
    poller._task = asyncio.create_task(exhausted())
    poller._task.add_done_callback(poller._observe_task)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert await poller.stop() == {"stopped": False, "error": "OSError"}
    assert sum("Controlled Telegram poller failed" in record.message for record in caplog.records) == 1


@pytest.mark.asyncio
async def test_unfinished_poll_cannot_produce_transfer_receipt(tmp_path):
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    started = asyncio.Event()
    release = asyncio.Event()
    class Bot:
        async def get_updates(self, **_kwargs):
            started.set()
            await release.wait()
            return []
    class Queue:
        async def join(self):
            pass
    class App:
        bot = Bot()
        update_queue = Queue()
    poller = ControlledPoller(App(), journal, timeout=0.02)
    await poller.start()
    await started.wait()
    try:
        assert await poller.stop() == {"stopped": False, "error": "PollDrainTimeout"}
        assert poller.running
    finally:
        release.set()
        task = poller._task
        assert task is not None
        await task


@pytest.mark.asyncio
async def test_replay_quarantines_corrupt_row_and_continues(tmp_path):
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    journal.record_response(b'{"ok":true,"result":[{"update_id":9},{"update_id":10}]}')
    with journal._connect() as db:
        db.execute("UPDATE telegram_updates SET raw_update=? WHERE update_id=9", (b'{"update_id":9,"message":[]}',))
    received = []
    class Queue:
        async def put(self, update):
            received.append(update.update_id)
        async def join(self):
            pass
    class Bot:
        async def get_updates(self, **_kwargs):
            return []
    class App:
        bot = Bot()
        update_queue = Queue()
    poller = ControlledPoller(App(), journal)
    await poller.start()
    assert received == [10]
    assert await poller.stop() == {"stopped": True}
    with journal._connect() as db:
        assert db.execute("SELECT state FROM telegram_updates WHERE update_id=9").fetchone()[0] == "quarantined"


@pytest.mark.asyncio
async def test_stop_returns_failed_receipt_after_exhaustion(tmp_path):
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    class App:
        pass
    poller = ControlledPoller(App(), journal)
    async def exhausted():
        raise OSError("all retries exhausted")
    poller._task = asyncio.create_task(exhausted())
    await asyncio.sleep(0)
    assert await poller.stop() == {"stopped": False, "error": "OSError"}


@pytest.mark.asyncio
@pytest.mark.parametrize("write_fails", [False, True])
async def test_lifecycle_write_cannot_delay_or_kill_polling(tmp_path, monkeypatch, caplog, write_fails):
    import os
    import sqlite3
    from types import SimpleNamespace
    from gateway.generation import GenerationIdentity

    monkeypatch.setattr("gateway.generation._boot_id", lambda: "fixture")
    coordinator = GenerationCoordinator(tmp_path)
    monkeypatch.setattr("gateway.status._get_process_start_time", lambda pid: 123)
    owner = GenerationIdentity.create(release_sha="r", label="r", boot_id="fixture",
                                      start_fingerprint=f"{os.getpid()}:123")
    coordinator.register(owner, state="serving")
    coordinator.acquire_lease("active_generation", owner.id)
    journal = PollingJournal(coordinator, "123456:LOCAL_ONLY")
    entered = threading.Event()
    release_write = threading.Event()
    requested = asyncio.Event()
    release_request = asyncio.Event()
    original = coordinator.record_poller_event

    def write(*args, **kwargs):
        if args[3] == "poller_started":
            entered.set()
            assert release_write.wait(5)
            if write_fails:
                raise sqlite3.OperationalError("database is locked")
        return original(*args, **kwargs)

    monkeypatch.setattr(coordinator, "record_poller_event", write)

    async def get_updates(**kwargs):
        requested.set()
        await release_request.wait()
        return []

    poller = ControlledPoller(SimpleNamespace(
        bot=SimpleNamespace(get_updates=get_updates), update_queue=asyncio.Queue()), journal)
    start = asyncio.create_task(poller.start())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # The writer is held until cleanup, so this does not depend on scheduler speed.
        await asyncio.wait_for(requested.wait(), 2)
        await asyncio.wait_for(asyncio.shield(start), 2)
        assert poller.running
    finally:
        release_write.set()
        release_request.set()
        await start
        assert await poller.stop() == {"stopped": True}
    events = [row["event"] for row in coordinator.poller_journal()]
    assert events == (["poller_stopped"] if write_fails else ["poller_started", "poller_stopped"])
    if write_fails:
        assert "polling lifecycle evidence could not be recorded" in caplog.text
        assert not coordinator.check_poller_journal()["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lease", ["missing", "foreign", "nonserving", "unknown_fingerprint"])
async def test_registered_process_must_prove_identity_before_replay_or_poll(tmp_path, monkeypatch, lease):
    import os
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from gateway.generation import GenerationIdentity

    monkeypatch.setattr("gateway.generation._boot_id", lambda: "fixture")
    coordinator = GenerationCoordinator(tmp_path)
    monkeypatch.setattr("gateway.status._get_process_start_time",
                        lambda pid: None if lease == "unknown_fingerprint" else 123)
    owner = GenerationIdentity.create(release_sha="r", label="r", boot_id="fixture",
                                      start_fingerprint=f"{os.getpid()}:123")
    coordinator.register(owner, state="standby" if lease == "nonserving" else "serving")
    if lease == "foreign":
        foreign = GenerationIdentity.create(release_sha="f", label="f", boot_id="fixture", pid=os.getpid() + 1)
        coordinator.register(foreign, state="serving")
        coordinator.acquire_lease("active_generation", foreign.id)
    elif lease != "missing":
        coordinator.acquire_lease("active_generation", owner.id)
    journal = PollingJournal(coordinator, "123456:LOCAL_ONLY")
    journal.record_response(b'{"ok":true,"result":[{"update_id":4}]}')
    app = SimpleNamespace(bot=SimpleNamespace(get_updates=AsyncMock()),
                          update_queue=SimpleNamespace(put=AsyncMock(), join=AsyncMock()))
    failures = []
    poller = ControlledPoller(app, journal, on_error=failures.append)
    try:
        with pytest.raises(RuntimeError, match="polling generation identity is not serving"):
            await poller.start()
        assert len(failures) == 1
        app.update_queue.put.assert_not_awaited()
        app.bot.get_updates.assert_not_awaited()
    finally:
        await poller.stop()
