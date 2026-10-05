"""Deferred outbox redelivery is one coalesced sweep, not a task per row (2026-10-05).

When a 6.7h Telegram penalty lifted, every row deferred during it shared one deadline. A
``redeliver`` task per row woke hundreds of concurrent ``recover()`` passes. Each pass re-logged
every held row (483k lines in 17 minutes), and each store call leaked a SQLite connection until
GC, so the gateway hit EMFILE. Receipts then failed, leaving 143 rows stuck in ``sending``.
"""

import asyncio
import gc
import logging
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

from gateway import outbox
from gateway.outbox import Outbox, _schedule_retry, _uncertain, recover
from gateway.platforms.base import SendResult


@pytest.fixture(autouse=True)
def _isolated_outbox_state(monkeypatch):
    monkeypatch.setattr(outbox, "_STORE_RETRY_DELAYS", (0.001, 0.001, 0.001))
    monkeypatch.setattr(outbox, "_SWEEP_IDLE_FLOOR_SECS", 0.01)
    for registry in (outbox._SWEEPS, outbox._IN_FLIGHT, outbox._HELD_LOGGED):
        registry.clear()
    yield
    for task, _wake in list(outbox._SWEEPS.values()):
        task.cancel()
    outbox._SWEEPS.clear()
    outbox._IN_FLIGHT.clear()
    outbox._HELD_LOGGED.clear()


class _Adapter:
    platform = "telegram"
    gateway_runner = None

    def __init__(self, result=None):
        self.sends: list[str] = []
        self._result = result

    async def send(self, chat_id, content, **_kwargs):
        self.sends.append(content)
        if self._result is not None:
            return self._result(content)
        return SendResult(success=True, message_id=f"m{len(self.sends)}")


def _defer(store: Outbox, content: str, retry_after: float):
    """A first attempt refused with ``retry_after``, exactly as a flood penalty leaves it."""
    row = store.enqueue("turn-" + content, "send", {"chat_id": "c", "content": content})
    assert store.begin_send(row)
    assert store.receipt(row, message_id=None, success=False, uncertain=False, retry_after=retry_after)
    return row


def _hold(store: Outbox, content: str):
    """A dispatch another process began but never receipted."""
    row = store.enqueue("held-" + content, "send", {"chat_id": "c", "content": content})
    assert store.begin_send(row)
    return row


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _fds() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.mark.asyncio
async def test_rows_sharing_a_deadline_wake_one_sweep_one_pass(tmp_path, monkeypatch, caplog):
    store = Outbox(tmp_path)
    rows = [_defer(store, f"reply-{n}", 0.2) for n in range(200)]
    held = [_hold(store, f"uncertain-{n}") for n in range(3)]
    adapter = _Adapter()

    passes = 0
    original = outbox._recover_locked

    async def counted(*args, **kwargs):
        nonlocal passes
        passes += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(outbox, "_recover_locked", counted)
    caplog.set_level(logging.ERROR, logger="gateway.outbox")
    gc.disable()
    try:
        before = _fds()
        for _ in rows:  # every deferred send used to schedule its own retry task
            await _schedule_retry(store, adapter)
        assert len(outbox._SWEEPS) == 1
        await _until(lambda: len(adapter.sends) == len(rows))
        await _until(lambda: not outbox._SWEEPS)
        # Connections are closed per operation, not left for GC with their WAL/SHM descriptors.
        assert _fds() - before < 20
    finally:
        gc.enable()

    assert sorted(adapter.sends) == sorted(f"reply-{n}" for n in range(200))
    assert passes == 1
    assert {r.state for r in store.all_rows() if r.turn_id.startswith("turn-")} == {"delivered"}
    held_lines = [r.getMessage() for r in caplog.records if "Held ambiguous" in r.getMessage()]
    assert len(held_lines) == len(held)

    await recover(store, adapter, startup=False)  # a later pass does not repeat them
    assert len([r for r in caplog.records if "Held ambiguous" in r.getMessage()]) == len(held)


@pytest.mark.asyncio
async def test_concurrent_recover_passes_are_serialized_per_store(tmp_path):
    store = Outbox(tmp_path)
    for n in range(20):
        _defer(store, f"r{n}", 0.0)
    active = peak = 0

    class Slow(_Adapter):
        async def send(self, chat_id, content, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            active -= 1
            return await super().send(chat_id, content, **kwargs)

    adapter = Slow()
    await asyncio.gather(*(recover(store, adapter, startup=False) for _ in range(10)))
    assert peak == 1
    assert sorted(adapter.sends) == sorted(f"r{n}" for n in range(20))


@pytest.mark.asyncio
async def test_row_in_flight_in_this_process_is_not_reported_held(tmp_path, caplog):
    store = Outbox(tmp_path)
    row = _hold(store, "sending-now")
    outbox._IN_FLIGHT.add((store.path, row.idempotency_key))
    caplog.set_level(logging.ERROR, logger="gateway.outbox")
    assert await recover(store, _Adapter(), startup=False) == (0, 0)
    assert not [r for r in caplog.records if "Held ambiguous" in r.getMessage()]


@pytest.mark.asyncio
async def test_receipt_survives_transient_descriptor_exhaustion(tmp_path, monkeypatch):
    store = Outbox(tmp_path)
    _defer(store, "late", 0.0)
    original = store.receipt
    failures = iter([True, True])

    def flaky(*args, **kwargs):
        if next(failures, False):
            raise sqlite3.OperationalError("unable to open database file")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "receipt", flaky)
    adapter = _Adapter()
    assert await recover(store, adapter, startup=False) == (1, 0)
    assert [r.state for r in store.all_rows()] == ["delivered"]


@pytest.mark.asyncio
async def test_unwritable_receipt_holds_that_row_and_the_pass_continues(tmp_path, monkeypatch, caplog):
    store = Outbox(tmp_path)
    first = _defer(store, "first", 0.0)
    _defer(store, "second", 0.0)
    original = store.receipt

    def broken_for_first(row, **kwargs):
        if row.idempotency_key == first.idempotency_key:
            raise sqlite3.OperationalError("unable to open database file")
        return original(row, **kwargs)

    monkeypatch.setattr(store, "receipt", broken_for_first)
    adapter = _Adapter()
    caplog.set_level(logging.ERROR, logger="gateway.outbox")
    await recover(store, adapter, startup=False)
    assert adapter.sends == ["first", "second"]
    states = {r.payload["content"]: r.state for r in store.all_rows()}
    assert states == {"first": "sending", "second": "delivered"}  # held, never resent
    assert any("could not be written" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_descriptor_exhaustion_before_the_wire_is_unsent_not_ambiguous(tmp_path):
    store = Outbox(tmp_path)
    _defer(store, "no-fd", 0.0)

    class NoDescriptors(_Adapter):
        async def send(self, chat_id, content, **kwargs):
            raise OSError(24, "Too many open files")

    await recover(store, NoDescriptors(), startup=False)
    assert [r.state for r in store.all_rows()] == ["failed_unsent"]
    assert _uncertain(SendResult(False, error="Network error: [Errno 24] Too many open files",
                                 retryable=True)) is False
    assert _uncertain(SendResult(False, error="network timeout", retryable=True)) is True


@pytest.mark.asyncio
async def test_an_earlier_deadline_wakes_the_parked_sweep(tmp_path):
    store = Outbox(tmp_path)
    _defer(store, "later", 30.0)
    adapter = _Adapter()
    await _schedule_retry(store, adapter)
    (task, _wake), = outbox._SWEEPS.values()
    _defer(store, "sooner", 0.05)
    await _schedule_retry(store, adapter)
    await _until(lambda: adapter.sends == ["sooner"])
    assert list(outbox._SWEEPS.values())[0][0] is task  # still the one sweep, parked for "later"


@pytest.mark.asyncio
async def test_sweep_survives_a_failed_pass(tmp_path, monkeypatch):
    store = Outbox(tmp_path)
    _defer(store, "eventually", 0.0)
    monkeypatch.setattr(outbox, "_SWEEP_ERROR_BACKOFF_SECS", (0.01,))
    original = outbox._recover_locked
    calls = 0

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("unable to open database file")
        return await original(*args, **kwargs)

    monkeypatch.setattr(outbox, "_recover_locked", fail_once)
    adapter = _Adapter()
    await _schedule_retry(store, adapter)
    await _until(lambda: adapter.sends == ["eventually"])


def test_store_operations_close_their_connections(tmp_path):
    store = Outbox(tmp_path)
    store.enqueue("t", "send", {"chat_id": "c", "content": "x"})
    gc.disable()
    try:
        before = _fds()
        for _ in range(100):
            store.pending()
            store.scheduled()
            store.ambiguous()
            store.next_retry_at()
        assert _fds() - before < 5
    finally:
        gc.enable()


def test_retry_identity_uses_platform_and_profile():
    adapter = _Adapter()
    adapter.gateway_runner = SimpleNamespace(_profile_adapters={"work": {"telegram": adapter}})
    assert outbox._retry_profile(adapter) == "work"
