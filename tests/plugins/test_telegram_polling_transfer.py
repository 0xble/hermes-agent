"""Durable Telegram polling boundary and transfer invariants."""
import asyncio
import json

import pytest
from telegram import Update

from gateway.generation import GenerationCoordinator
from plugins.platforms.telegram.polling_transfer import PollingJournal, ControlledPoller


def test_wire_journal_commits_before_offset_and_replays_unaccepted(tmp_path):
    coordinator = GenerationCoordinator(tmp_path)
    journal = PollingJournal(coordinator, "123456:LOCAL_ONLY")
    batch = [{"update_id": 10, "message": {"text": "a"}}, {"update_id": 11, "message": {"text": "b"}}]
    journal.record_response(json.dumps({"ok": True, "result": batch}).encode())
    assert journal.safe_offset() == 12
    assert [item["update_id"] for item in journal.pending()] == [10, 11]
    assert journal.claim(10)
    assert not journal.claim(10)
    assert [item["update_id"] for item in journal.pending()] == [11]
    journal.accept(10)
    journal.record_response(json.dumps({"ok": True, "result": batch}).encode())
    assert [item["update_id"] for item in journal.pending()] == [11]
    assert not journal.claim(10)


def test_idle_reset_allows_nonmonotonic_ids_and_prunes_accepted_rows(tmp_path, monkeypatch):
    from plugins.platforms.telegram import polling_transfer

    clock = [1_000_000.0]
    monkeypatch.setattr(polling_transfer.time, "time", lambda: clock[0])
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    journal.record_response(b'{"ok":true,"result":[{"update_id":900000}]}')
    assert journal.claim(900000)
    journal.accept(900000)
    clock[0] += 8 * 24 * 60 * 60
    assert journal.safe_offset() == 0
    journal.record_response(b'{"ok":true,"result":[{"update_id":5}]}')
    assert journal.safe_offset() == 6
    assert [row["update_id"] for row in journal.pending()] == [5]
    with journal._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM telegram_updates").fetchone()[0] == 1


def test_retention_runs_on_empty_poll_response(tmp_path, monkeypatch):
    from plugins.platforms.telegram import polling_transfer

    clock = [1_000_000.0]
    monkeypatch.setattr(polling_transfer.time, "time", lambda: clock[0])
    journal = PollingJournal(GenerationCoordinator(tmp_path), "123456:LOCAL_ONLY")
    journal.record_response(b'{"ok":true,"result":[{"update_id":5}]}')
    assert journal.claim(5)
    journal.accept(5)
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
            journal.claim(update.update_id)
            journal.accept(update.update_id)

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
