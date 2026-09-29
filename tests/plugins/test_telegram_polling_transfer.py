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

    poller = ControlledPoller(App(), journal, timeout=1)
    await poller.start()
    await asyncio.wait_for(request_started.wait(), 2)
    stop = asyncio.create_task(poller.stop())
    await asyncio.sleep(0.02)
    assert not stop.done()
    release_request.set()
    await asyncio.wait_for(stop, 2)
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
async def test_journal_write_runs_off_loop_and_queue_join_is_bounded(tmp_path):
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

    class Queue:
        async def put(self, update):
            pass
        async def join(self):
            await asyncio.Future()
    class App:
        bot = object()
        update_queue = Queue()
    poller = ControlledPoller(App(), journal, timeout=0.02)
    journal.record_response(b'{"ok":true,"result":[{"update_id":5}]}')
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(poller.start(), 1)


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
