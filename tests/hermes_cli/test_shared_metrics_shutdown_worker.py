"""Shutdown must settle the turn's export before its final export accesses the same store."""

from __future__ import annotations

import threading

import pytest

from hermes_cli.observability import relay_shared_metrics
from hermes_cli.observability.shared_metrics_process import EXIT_FLUSH_TIMEOUT_S
from tests.hermes_cli.test_relay_shared_metrics_runtime import direct_runtime

pytestmark = pytest.mark.usefixtures("direct_runtime")


def test_shutdown_joins_the_inflight_export_before_its_final_export(monkeypatch):
    runtime = relay_shared_metrics._get_runtime()
    assert runtime is not None
    entered, release, observed = (threading.Event() for _ in range(3))
    overlap = []
    exported = []
    export = runtime._export

    def parked_export():
        if not entered.is_set():
            with runtime.subscriber.store._write():
                entered.set()
                assert release.wait(3)
        elif not release.is_set():
            overlap.append(True)
        export()
        exported.append(threading.current_thread().name)
        if overlap:
            observed.set()

    monkeypatch.setattr(runtime, "_export", parked_export)
    runtime._schedule_flush_and_export("test export failed")
    assert entered.wait(3)
    worker = runtime._flush_thread
    assert worker is not None
    join = worker.join

    def observed_join(timeout=None):
        observed.set()
        return join(timeout)

    monkeypatch.setattr(worker, "join", observed_join)
    shutdown = threading.Thread(target=runtime.shutdown)
    shutdown.start()
    try:
        assert observed.wait(3), "shutdown neither joined the worker nor exported"
        assert not overlap, "shutdown started another export while the worker held the write transaction"
    finally:
        release.set()
        join(3)
        shutdown.join(3)
    assert not worker.is_alive()
    assert not shutdown.is_alive()
    assert len(exported) == 2
    assert not runtime._registered


def test_shutdown_join_timeout_leaves_the_final_flush_with_its_worker(monkeypatch):
    runtime = relay_shared_metrics._get_runtime()
    assert runtime is not None
    entered, release = threading.Event(), threading.Event()
    exported, timeouts = [], []
    export = runtime._export

    def parked_export():
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        exported.append(threading.current_thread())
        export()

    monkeypatch.setattr(runtime, "_export", parked_export)
    runtime._schedule_flush_and_export("test export failed")
    assert entered.wait(3)
    worker = runtime._flush_thread
    assert worker is not None
    join = worker.join
    monkeypatch.setattr(worker, "join", lambda timeout=None: timeouts.append(timeout))
    try:
        runtime.shutdown()
        assert timeouts and timeouts[0] == EXIT_FLUSH_TIMEOUT_S
        assert not exported, "a timed-out join must not launch a concurrent final export"
        assert runtime._registered, "the queued final flush still needs its subscriber"
        assert not runtime._flush_shutdown_complete.is_set()
        runtime._schedule_flush_and_export("late task flush")
    finally:
        release.set()
        join(3)
    assert not worker.is_alive()
    assert exported == [worker, worker]
    assert not runtime._registered
    assert runtime._flush_shutdown_complete.is_set()
