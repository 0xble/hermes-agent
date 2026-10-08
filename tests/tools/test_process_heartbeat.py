"""Heartbeat notifications for long background processes.

A ``heartbeat`` on a background process emits a periodic "still running + output since the last
heartbeat" event on the completion queue so the agent stays current on a long bounded job (merge
train, full suite, deploy) without polling. Invariants: each heartbeat carries only NEW output,
heartbeats stop at exit, and the normal completion notice still fires.
"""
import json
import queue
import threading
import time

import pytest

import tools.process_registry as pr
from tools.process_registry import ProcessRegistry


def _drain(q: "queue.Queue") -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def _wait_until(pred, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


@pytest.mark.platforms("linux")
def test_heartbeat_carries_only_new_output_and_stops_at_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "HEARTBEAT_MIN_SECONDS", 1)
    monkeypatch.setattr(pr, "HEARTBEAT_TICK_SECONDS", 0.1)
    registry = ProcessRegistry()
    session = registry.spawn_local(
        "echo first; while [ ! -e second-ready ]; do sleep 0.05; done; "
        "echo second; while [ ! -e finish-ready ]; do sleep 0.05; done",
        cwd=str(tmp_path),
    )
    session.notify_on_complete = True
    # Arm after the reader has captured output, as happens when a fast child
    # prints before terminal dispatch has returned to configure its heartbeat.
    assert _wait_until(lambda: "first" in session.output_buffer, timeout=20)
    assert registry.arm_heartbeat(session, 1) == 1
    assert _wait_until(
        lambda: any(e.get("type") == "heartbeat" and "first" in e["output"]
                    for e in list(registry.completion_queue.queue)), timeout=10)
    (tmp_path / "second-ready").touch()
    assert _wait_until(
        lambda: any(e.get("type") == "heartbeat" and "second" in e["output"]
                    for e in list(registry.completion_queue.queue)), timeout=10)
    (tmp_path / "finish-ready").touch()
    assert session._completion_event.wait(timeout=20)
    events = _drain(registry.completion_queue)
    beats = [e for e in events if e["type"] == "heartbeat"]
    completion = [e for e in events if e["type"] == "completion"]

    assert len(beats) >= 2, events
    assert [b["seq"] for b in beats] == list(range(1, len(beats) + 1))
    assert all(b["session_id"] == session.id and b["interval"] == 1 for b in beats)
    # Output is a delta: every produced line appears in exactly one heartbeat, never twice.
    joined = "".join(b["output"] for b in beats)
    assert joined.count("first") == 1 and joined.count("second") == 1, [b["output"] for b in beats]
    assert len(completion) == 1
    # Heartbeats never outlive the process: nothing after the completion notice.
    assert events.index(completion[0]) > events.index(beats[-1])


@pytest.mark.platforms("linux")
def test_heartbeat_due_at_exit_cannot_arrive_after_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "HEARTBEAT_MIN_SECONDS", 1)
    monkeypatch.setattr(pr, "HEARTBEAT_TICK_SECONDS", 0.1)
    registry = ProcessRegistry()
    session = registry.spawn_local(
        "while [ ! -e finish-ready ]; do sleep 0.05; done", cwd=str(tmp_path))
    session.notify_on_complete = True
    due = threading.Event()
    resume = threading.Event()
    returned = threading.Event()
    emit = registry._emit_heartbeat

    def delay_due_heartbeat(target, now):
        due.set()
        try:
            assert resume.wait(timeout=20)
            emit(target, now)
        finally:
            returned.set()

    monkeypatch.setattr(registry, "_emit_heartbeat", delay_due_heartbeat)
    try:
        registry.arm_heartbeat(session, 1)
        assert due.wait(timeout=10)
        (tmp_path / "finish-ready").touch()
        assert session._completion_event.wait(timeout=20)
    finally:
        resume.set()
    assert returned.wait(timeout=10)
    assert [event["type"] for event in _drain(registry.completion_queue)] == ["completion"]


@pytest.mark.platforms("linux")
def test_first_heartbeat_carries_output_produced_before_arming(tmp_path, monkeypatch):
    """The terminal tool arms the heartbeat after its spawn bookkeeping; whatever the process
    printed in that gap belongs to the first heartbeat, not to nobody (CI: 'first' vanished)."""
    monkeypatch.setattr(pr, "HEARTBEAT_MIN_SECONDS", 1)
    monkeypatch.setattr(pr, "HEARTBEAT_TICK_SECONDS", 0.1)
    registry = ProcessRegistry()
    session = registry.spawn_local("echo first; sleep 3", cwd=str(tmp_path))
    session.notify_on_complete = True
    assert _wait_until(lambda: "first" in registry.poll(session.id).get("output_preview", ""), timeout=10)
    registry.arm_heartbeat(session, 1)

    assert _wait_until(lambda: any(e.get("type") == "heartbeat" for e in list(registry.completion_queue.queue)),
                       timeout=10)
    first_beat = next(e for e in _drain(registry.completion_queue) if e["type"] == "heartbeat")
    assert "first" in first_beat["output"], first_beat


def test_terminal_dispatch_heartbeat_implies_notify_and_foreground_runs_without_it(monkeypatch):
    """A foreground call carrying an explicit positive heartbeat (the shape models copy from a
    background call) runs once with the heartbeat dropped: refusing it only produced
    identical-call retry loops. Notification intent (`notify=true`) stays refused."""
    from tools import terminal_tool as tt

    captured = {}

    def fake_terminal_tool(**kwargs):
        captured.update(kwargs)
        return json.dumps({"output": "Background process started", "session_id": "proc_x", "exit_code": 0})

    monkeypatch.setattr(tt, "terminal_tool", fake_terminal_tool)
    dispatch = tt._handle_terminal
    fg = json.loads(dispatch({"command": "sleep 1", "background": False, "notify": False, "heartbeat": 120}))
    assert not fg.get("error")
    assert captured["background"] is False and captured["heartbeat"] == 0
    assert captured["notify_on_complete"] is False

    captured.clear()
    fg_notify = json.loads(dispatch({"command": "sleep 1", "notify": True}))
    assert fg_notify.get("error") and "background" in fg_notify["error"] and not captured

    bg = json.loads(dispatch({"command": "sleep 1", "background": True, "heartbeat": 120}))
    assert "error" not in bg or not bg["error"]
    assert captured["heartbeat"] == 120 and captured["notify_on_complete"] is True
