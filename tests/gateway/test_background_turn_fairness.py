"""Goal/loop wakes make progress despite ungated load and hot process-watch retries."""

from contextlib import nullcontext
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import turn_origin_for_event
from gateway.wake import admit_internal_event
from tests.gateway.test_background_turn_admission import _event, _runner


def _watch_queue(runner, adapter, monkeypatch):
    """Keep routing external to the real watch drain, wake admission and live turn gate."""
    key = "agent:main:telegram:dm:watch"
    evt = {"type": "watch_match", "session_key": key, "session_id": "proc", "text": "ready"}
    completions = Queue()
    completions.put(evt)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr("gateway.run_notifications.time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(runner, "_completion_event_scope", lambda _evt: nullcontext())
    monkeypatch.setattr(runner, "_completion_held_by_stop", lambda _evt: False)
    monkeypatch.setattr(runner, "_load_background_notifications_mode", lambda: "all")
    monkeypatch.setattr(runner, "_build_process_event_source", lambda _evt: _event(key).source)
    monkeypatch.setattr(runner, "_resolve_injection_adapter", lambda *_args: adapter)
    monkeypatch.setattr("gateway.run._format_gateway_process_notification", lambda _evt: "ready")
    monkeypatch.setattr("hermes_cli.session_controls.internal_wake_is_current", lambda _evt: True)
    inject = AsyncMock(wraps=runner._inject_watch_notification)
    monkeypatch.setattr(runner, "_inject_watch_notification", inject)
    return completions, evt, clock, inject


@pytest.mark.asyncio
@pytest.mark.parametrize("wake_stamp", ["goal_continuation", "loop_wakeup"])
async def test_due_wake_wins_a_slot_under_ungated_load_and_tight_watch_retries(monkeypatch, wake_stamp):
    runner, adapter = _runner(8)
    completions, evt, clock, _inject = _watch_queue(runner, adapter, monkeypatch)
    for i in range(8):
        relay = _event(f"relay{i}")
        relay.metadata["notification_origin"] = "relay_delivery"
        await adapter.handle_message(relay)
        await adapter.handle_message(_event(f"process{i}"))

    # A full gate refuses the watch, retaining the very same event for retry.
    await runner._drain_watch_notifications(completions)
    assert completions.qsize() == 1
    adapter.end_turn("process0")
    for i in range(1, 5):
        clock.now = float(i)
        for _ in range(25):
            await runner._drain_watch_notifications(completions)

    # The native watcher polls after 5 seconds. Relays still run and process retries
    # must leave this freed slot available instead of reacquiring it in a hot loop.
    clock.now = 5.0
    due = _event("due")
    due.metadata = {"gateway_session_key": "due", wake_stamp: True}
    assert turn_origin_for_event(due) == "goal_loop"
    await admit_internal_event(adapter, due)
    assert due._gateway_accepted is True
    assert all(f"relay{i}" in adapter.background for i in range(8))
    assert completions.qsize() == 1

    # The process event is delayed, never dropped or starved in the other direction.
    adapter.end_turn("due")
    clock.now = 10.0
    await runner._drain_watch_notifications(completions)
    assert adapter.admitted.count(evt["session_key"]) == 1
    assert completions.empty()


@pytest.mark.asyncio
async def test_watch_retry_deadline_keeps_one_event_and_preserves_the_resume_bound(monkeypatch):
    runner, adapter = _runner(1)
    completions, evt, clock, inject = _watch_queue(runner, adapter, monkeypatch)
    resume = runner._build_startup_resume_event(_event("resume").source, "marker")
    resume.metadata["gateway_session_key"] = "resume"
    runner._startup_resume_inflight = {"resume"}
    await adapter.handle_message(resume)
    assert turn_origin_for_event(resume) == "unknown"

    await runner._drain_watch_notifications(completions)
    assert completions.qsize() == 1  # The live resume still owns the only slot.
    assert inject.await_count == 1
    adapter.end_turn("resume")
    for i in range(100):
        clock.now = i / 10
        await runner._drain_watch_notifications(completions)
        assert completions.qsize() == 1
    assert inject.await_count == 1  # No attempts before the retry deadline.
    assert completions.get_nowait() is evt
    completions.put(evt)

    clock.now = 10.0
    await runner._drain_watch_notifications(completions)
    assert inject.await_count == 2
    assert adapter.admitted.count(evt["session_key"]) == 1
    assert completions.empty()
