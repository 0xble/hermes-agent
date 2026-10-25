"""Rapid recall warming owns one worker and only the newest queued query."""

import threading
from unittest.mock import Mock

from plugins.memory.hindsight import HindsightMemoryProvider


def test_prefetch_coalesces_to_latest_query_and_fences_discard_and_shutdown(monkeypatch):
    provider = HindsightMemoryProvider()
    provider._auto_recall = True
    provider._prefetch_waits_for_retain = False
    entered, release = threading.Event(), threading.Event()
    calls = []
    workers = []
    from plugins.memory.hindsight import spawn_context_thread

    def spawn(*a, **kw):
        worker = spawn_context_thread(*a, **kw)
        workers.append(worker)
        return worker

    def recall(query):
        calls.append(query)
        if query == "first":
            entered.set()
            assert release.wait(5)
        return query, 1

    monkeypatch.setattr("plugins.memory.hindsight.spawn_context_thread", spawn)
    monkeypatch.setattr(provider, "_do_recall", recall)
    try:
        provider.queue_prefetch("first")
        assert entered.wait(5)
        for query in ["second", "third", "fourth", "latest"]:
            provider.queue_prefetch(query)
        assert len(workers) == 1
        release.set()
        workers[0].join(5)
        assert not workers[0].is_alive()
        assert calls == ["first", "latest"]
        assert "latest" in provider.prefetch("next")

        # An obsolete pending query must not run after a session switch/discard.
        entered.clear()
        release.clear()
        provider.queue_prefetch("first")
        assert entered.wait(5)
        provider.queue_prefetch("discarded")
        provider.discard_prefetch()
        release.set()
        provider._join_prefetch(5)
        assert "discarded" not in calls
        assert provider.prefetch("next") == ""
        provider.shutdown()
        provider.queue_prefetch("after shutdown")
        provider._join_prefetch(5)
        assert "after shutdown" not in calls
    finally:
        release.set()
        provider.shutdown()


class _WorkerAbort(BaseException):
    pass


def test_prefetch_worker_base_exception_resets_running_flag(monkeypatch):
    """A BaseException escaping the worker must not leave every later prefetch waiting on it."""
    provider = HindsightMemoryProvider()
    provider._auto_recall = True
    provider._prefetch_waits_for_retain = False
    calls = []

    def recall(query):
        calls.append(query)
        if query == "abort":
            raise _WorkerAbort()
        return query, 1

    monkeypatch.setattr(provider, "_do_recall", recall)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    try:
        provider.queue_prefetch("abort")
        provider._join_prefetch(5)
        assert provider._prefetch_running is False
        provider.queue_prefetch("after")
        provider._join_prefetch(5)
        assert calls == ["abort", "after"]
        assert "after" in provider.prefetch("next")
    finally:
        provider.shutdown()
