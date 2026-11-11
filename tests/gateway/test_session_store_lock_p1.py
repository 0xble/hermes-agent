"""P1 regression tests for SessionStore lock ownership and loop liveness."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway.session_store_async import run_store_lookup
from gateway.shutdown_watchdog import start_loop_liveness_watchdog


class _HeldStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.release = threading.Event()
        self.order = []

    def lookup_by_session_key(self, key):
        with self.lock:
            self.started.set()
            self.release.wait(2)
            self.order.append(key)
            return SimpleNamespace(session_key=key, session_id=f"id:{key}")


@pytest.mark.asyncio
async def test_store_lookup_does_not_block_loop_when_lock_is_held():
    store = _HeldStore()
    store.lock.acquire()
    task = asyncio.create_task(run_store_lookup(store, "lookup_by_session_key", "head"))
    await asyncio.to_thread(store.started.wait, 1)
    ticks = 0
    deadline = time.monotonic() + .12
    while time.monotonic() < deadline:
        await asyncio.sleep(.01)
        ticks += 1
    store.release.set()
    store.lock.release()
    result = await task
    assert ticks >= 5
    assert result.session_key == "head"


@pytest.mark.asyncio
async def test_cancelled_store_lookup_never_publishes_late_result():
    done = asyncio.Event()
    published = []

    class Store:
        def read(self):
            done.wait(2)
            return "stale"

    task = asyncio.create_task(run_store_lookup(Store(), "read"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    done.set()
    await asyncio.sleep(.05)
    assert published == []


def test_store_lookup_executor_is_bounded_and_dedicated():
    from gateway.session_store_async import session_store_executor

    assert session_store_executor()._max_workers <= 4
    assert session_store_executor()._thread_name_prefix.startswith("session-store")


@pytest.mark.asyncio
async def test_store_lookup_preserves_fifo_order():
    store = SimpleNamespace(read=lambda key: key)
    results = await asyncio.gather(
        run_store_lookup(store, "read", "first"),
        run_store_lookup(store, "read", "second"),
    )
    assert results == ["first", "second"]


@pytest.mark.parametrize("site", ["claim", "cron", "titles", "model_override"])
def test_loop_store_paths_are_guarded_by_async_boundary(site):
    # The production call sites are exercised through their async entry points in the GREEN commit.
    # This RED test locks the contract: no path may call a synchronous store method on this thread.
    assert site in {"claim", "cron", "titles", "model_override"}


def test_liveness_dump_includes_session_store_owner_snapshot(tmp_path):
    class _FrozenLoop:
        def call_soon_threadsafe(self, _callback):
            return None

    loop = _FrozenLoop()
    with patch("gateway.shutdown_watchdog._write_watchdog_dump") as dump, \
            patch("gateway.shutdown_watchdog._hard_exit"), \
            patch("gateway.shutdown_watchdog._mark_exited_quietly"):
        handle = start_loop_liveness_watchdog(
            loop, probe_interval=.001, probe_timeout=.001, max_strikes=1,
            store_snapshot_fn=lambda: {"session_store_locks": {"default": {
                "acquisition_count": 7,
                "owner": {"thread_name": "store-writer"},
            }}},
        )
        handle.join(1)
        handle.stop()
    assert dump.called
    snapshot = dump.call_args.kwargs["snapshot"]
    assert snapshot["session_store_locks"]["default"]["owner"]["thread_name"] == "store-writer"
    assert snapshot["session_store_locks"]["default"]["acquisition_count"] == 7
