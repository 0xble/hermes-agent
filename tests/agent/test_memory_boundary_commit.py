"""Tests for MemoryManager.commit_session_boundary_async.

The /new session boundary must deliver on_session_end (old-session
extraction) strictly BEFORE on_session_switch (provider rebinding to the
new session), without blocking the caller. Both hooks run as one task on
the manager's single serialized background worker.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class _RecordingProvider(MemoryProvider):
    """Provider that records hook invocations with thread identity."""

    def __init__(self, end_delay: float = 0.0):
        self.calls: List[tuple] = []
        self._end_delay = end_delay
        self._caller_thread_ids: List[int] = []

    # Required ABC surface (minimal no-ops)
    @property
    def name(self) -> str:
        return "recorder"

    def is_available(self) -> bool:
        return True

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def initialize(self, agent: Any = None, **kwargs) -> bool:  # type: ignore[override]
        return True

    def build_system_prompt(self) -> str:  # type: ignore[override]
        return ""

    def sync_turn(self, user_content: str, assistant_content: str, **kwargs) -> None:  # type: ignore[override]
        self.calls.append(("sync_turn", kwargs.get("session_id", "")))

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._end_delay:
            time.sleep(self._end_delay)
        self._caller_thread_ids.append(threading.get_ident())
        self.calls.append(("end", list(messages)))

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self.calls.append(("switch", new_session_id, kwargs.get("reset")))


def _make_manager(provider: _RecordingProvider) -> MemoryManager:
    mm = MemoryManager()
    mm._providers.append(provider)  # bypass add_provider validation for the stub
    return mm


def test_boundary_commit_delivers_end_strictly_before_switch():
    """Even with a slow (LLM-like) extraction, switch waits for end."""
    provider = _RecordingProvider(end_delay=0.15)
    mm = _make_manager(provider)

    msgs = [{"role": "user", "content": "old turn"}]
    mm.commit_session_boundary_async(
        msgs, new_session_id="new-sid", parent_session_id="old-sid"
    )
    # DETERMINISTIC non-blocking witness — replaces `assert elapsed < 0.1`.
    #
    # The old form timed `commit_session_boundary_async` and required it under
    # 100ms, which makes the scheduler part of the assertion: thread startup
    # alone can exceed that on a loaded box, flipping the inequality with
    # nothing wrong in the code under test.
    #
    # The real contract is that the caller returns WITHOUT waiting for the slow
    # extraction. Assert it directly: the background `on_session_end` sleeps
    # 0.15s before recording anything, so if the caller had blocked on it, the
    # provider would already have recorded the "end" call by the time we get
    # here. An empty call list is a positive witness that /new was not gated.
    assert provider.calls == [], (
        "commit_session_boundary_async blocked on the slow extraction: "
        f"provider already recorded {provider.calls} before the caller returned"
    )

    assert mm.flush_pending(timeout=30)

    kinds = [c[0] for c in provider.calls]
    assert kinds == ["end", "switch"], f"ordering violated: {provider.calls}"
    assert provider.calls[0] == ("end", msgs)
    assert provider.calls[1] == ("switch", "new-sid", True)
    # And it genuinely ran off the caller's thread.
    assert provider._caller_thread_ids[0] != threading.get_ident()




@pytest.mark.parametrize("queued_session_id", ["new-sid", "old-sid", "other-sid", ""])
def test_boundary_commit_preserves_only_destination_prefetch(queued_session_id):
    """A new turn can queue recall while /new is still extracting the old session."""
    extracting, release = threading.Event(), threading.Event()

    class _BufferingProvider(_RecordingProvider):
        def __init__(self):
            super().__init__()
            self.buffer = ""

        def on_session_end(self, messages):
            extracting.set()
            assert release.wait(5), "old-session extraction was not released"
            super().on_session_end(messages)

        def queue_prefetch(self, query, *, session_id=""):
            self.calls.append(("queue", query, session_id))
            self.buffer = f"recall for: {query}"

        def prefetch(self, query, *, session_id=""):
            result, self.buffer = self.buffer, ""
            return result

        def discard_prefetch(self):
            self.calls.append(("discard",))
            self.buffer = ""

    provider = _BufferingProvider()
    mm = _make_manager(provider)
    mm.initialize_all("old-sid")
    try:
        mm.queue_prefetch_all("previous topic", session_id="old-sid")
        assert mm.flush_pending(timeout=5)
        assert provider.buffer  # the switch must still drop the old session's buffer
        provider.calls.clear()
        mm.commit_session_boundary_async(
            [{"role": "user", "content": "old turn"}], new_session_id="new-sid"
        )
        assert extracting.wait(5), "boundary did not reach old-session extraction"
        mm.queue_prefetch_all("queued topic", session_id=queued_session_id)
        assert provider.calls == []  # dispatch is waiting behind end -> switch
        release.set()
        assert mm.flush_pending(timeout=5)

        expected_calls = [
            ("end", [{"role": "user", "content": "old turn"}]),
            ("discard",),
            ("switch", "new-sid", True),
        ]
        expected_recall = ""
        if queued_session_id == "new-sid":
            expected_calls.append(("queue", "queued topic", "new-sid"))
            expected_recall = "recall for: queued topic"
        assert provider.calls == expected_calls
        assert mm.prefetch_all("follow-up topic", session_id="new-sid") == expected_recall
    finally:
        release.set()
        mm.shutdown_all()


@pytest.mark.parametrize(
    ("new_session_id", "rewound"),
    [("same-sid", True), ("same-sid", False), ("new-sid", True)],
    ids=["undo", "in-place-compaction", "rewound-destination"],
)
def test_session_switch_invalidates_queued_prefetch_after_transcript_change(new_session_id, rewound):
    """Undo and in-place compaction must not dispatch recall keyed on the pre-switch transcript."""
    syncing, release = threading.Event(), threading.Event()

    class _SlowSync(_RecordingProvider):
        def __init__(self):
            super().__init__()
            self.buffer = ""
            self.queued = []
            self.switched_to = []

        def sync_turn(self, *args, **kwargs):
            syncing.set()
            assert release.wait(5), "turn sync was not released"
            super().sync_turn(*args, **kwargs)

        def queue_prefetch(self, query, *, session_id=""):
            self.queued.append(query)
            self.buffer = f"recall for: {query}"

        def prefetch(self, query, *, session_id=""):
            result, self.buffer = self.buffer, ""
            return result

        def discard_prefetch(self):
            self.buffer = ""

        def on_session_switch(self, new_session_id, **kwargs):
            self.switched_to.append((new_session_id, kwargs.get("rewound", False)))

    provider = _SlowSync()
    mm = _make_manager(provider)
    mm.initialize_all("same-sid")
    try:
        mm.sync_all("undone topic", "Done.", session_id="same-sid")
        assert syncing.wait(5), "turn sync did not start"
        mm.queue_prefetch_all("undone topic", session_id=new_session_id)
        mm.on_session_switch(new_session_id, rewound=rewound)
        release.set()
        assert mm.flush_pending(timeout=5)

        assert provider.switched_to == [(new_session_id, rewound)]
        assert mm.prefetch_all("next turn", session_id=new_session_id) == ""
        assert provider.queued == []  # the invalidated query never reaches the provider

        mm.queue_prefetch_all("fresh topic", session_id=new_session_id)
        assert mm.flush_pending(timeout=5)
        assert mm.prefetch_all("follow-up", session_id=new_session_id) == "recall for: fresh topic"
    finally:
        release.set()
        mm.shutdown_all()


def test_boundary_commit_switch_still_fires_when_end_raises():
    """A failing provider extraction must not strand providers on the old sid."""

    class _ExplodingEndProvider(_RecordingProvider):
        def on_session_end(self, messages):  # type: ignore[override]
            raise RuntimeError("provider extraction blew up")

    provider = _ExplodingEndProvider()
    mm = _make_manager(provider)

    mm.commit_session_boundary_async([{"role": "user", "content": "x"}], new_session_id="new-sid")
    assert mm.flush_pending(timeout=5)

    assert ("switch", "new-sid", True) in provider.calls


