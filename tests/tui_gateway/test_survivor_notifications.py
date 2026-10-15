"""Real TUI consumer coverage for restart-survivor completion delivery."""

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession
from tools.process_registry_notifications import format_process_notification
from tui_gateway import server


def _survivor_registry(tmp_path, monkeypatch, session_key="tui-session"):
    home = tmp_path / "home"
    monkeypatch.setattr("tools.process_registry.get_hermes_home", lambda: home)
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_tui_survivor", command="make test", task_id=session_key,
        session_key=session_key, survive_restart=True, exited=True, exit_code=3,
        log_path=str(home / "process-runs" / "proc_tui_survivor" / "out.log"),
        delivered_path=str(home / "process-runs" / "proc_tui_survivor" / "delivered"),
    )
    registry._finished[session.id] = session
    event = {
        "type": "completion", "session_id": session.id, "session_key": session_key,
        "command": session.command, "exit_code": 3, "completion_reason": "exited",
        "output": "test output",
    }
    return registry, session, event


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("refusal", ["exception", "false"])
def test_failed_submit_retries_survivor_completion_once(tmp_path, monkeypatch, batch, refusal):
    registry, process, event = _survivor_registry(tmp_path, monkeypatch)
    processes, events = [process], [event]
    if batch:
        sibling = replace(process, id="proc_tui_sibling", command="make lint",
                          delivered_path=str(Path(process.delivered_path).parent.parent / "proc_tui_sibling" / "delivered"))
        registry._finished[sibling.id] = sibling
        processes.append(sibling)
        events.append(dict(event, session_id=sibling.id, command=sibling.command))
    session = {"history_lock": threading.RLock(), "running": False, "history": [], "session_key": "tui-session"}
    emitted = set()
    attempts = []
    accepted = []
    statuses = []
    now = [100.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(server, "_emit", lambda method, sid, payload=None: statuses.append((method, payload)))
    monkeypatch.setattr(server, "_background_notifications_off", lambda _: False)

    def submit(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            if refusal == "exception":
                raise RuntimeError("turn admission failed")
            # Actual admission refusals return False; no worker owns the turn.
            server._notif_release_turn(session)
            return False
        accepted.append(args)
        server._notif_release_turn(session)
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", submit)

    def handle(events):
        if batch:
            server._notif_handle_ready(
                "tui-sid", session, events, emitted, registry,
                format_process_notification, None, owned=True,
            )
        else:
            for evt in events:
                server._notif_handle_event(
                    "tui-sid", session, evt, emitted, registry,
                    format_process_notification, None, owned=True,
                )

    handle(events)
    assert len(attempts) == 1 and not accepted
    assert not session["running"]
    assert all(registry._survivor_delivery_state(proc) is None for proc in processes)
    assert all(registry._survivor_delivery_pending(proc) for proc in processes)
    assert registry.completion_queue.qsize() == len(events)
    assert not emitted

    retry = [registry.completion_queue.get_nowait() for _ in events]
    assert retry == events
    handle(retry)
    assert len(attempts) == 1 and not accepted
    retry = [registry.completion_queue.get_nowait() for _ in events]
    assert retry == events
    now[0] += 0.5
    handle(retry)
    assert len(attempts) == 2 and len(accepted) == 1
    assert all(registry._survivor_delivery_state(proc) == "delivered" for proc in processes)
    assert registry.completion_queue.empty()
    # Stale duplicates cannot start a second successful turn.
    handle(events)
    assert len(accepted) == 1
    assert sum(method == "status.update" for method, _payload in statuses) == 2 * len(events)


def test_busy_poller_keeps_survivor_for_post_turn_drain(tmp_path, monkeypatch):
    registry, process, event = _survivor_registry(tmp_path, monkeypatch)
    session = {"history_lock": threading.RLock(), "running": True, "history": [], "session_key": "tui-session"}
    status = []
    submitted = []
    monkeypatch.setattr(server, "_emit", lambda method, sid, payload: status.append((method, sid, payload)))
    monkeypatch.setattr(server, "_notif_claim_turn", lambda current: False)
    monkeypatch.setattr(server, "_notif_submit", lambda *args, **kwargs: submitted.append((args, kwargs)))
    monkeypatch.setattr("tools.async_delegation.claim_event_delivery", lambda evt, consumer: "claim")
    monkeypatch.setattr("tools.async_delegation.complete_event_delivery", lambda evt, claim: None)

    server._notif_handle_ready(
        "tui-sid", session, [event], set(), registry,
        format_process_notification, None,
    )

    assert status
    assert json.loads(Path(process.delivered_path).read_text(encoding="utf-8-sig"))["state"] == "claimed"
    session["running"] = False
    drained = registry.drain_notifications(session_key="tui-session", skip_poll_observed=False)

    assert [drained_event for drained_event, _text in drained] == [event]
    assert not submitted


def test_owned_post_turn_drain_emits_and_acks_survivor(tmp_path, monkeypatch):
    registry, process, event = _survivor_registry(tmp_path, monkeypatch)
    session = {"history_lock": threading.RLock(), "running": False, "history": [], "session_key": "tui-session"}
    status = []
    submitted = []
    monkeypatch.setattr(server, "_emit", lambda method, sid, payload: status.append((method, sid, payload)))
    monkeypatch.setattr(server, "_notif_claim_turn", lambda current: True)
    monkeypatch.setattr(server, "_notif_submit", lambda *args, **kwargs: submitted.append((args, kwargs)))
    monkeypatch.setattr("tools.async_delegation.claim_event_delivery", lambda evt, consumer: "claim")
    monkeypatch.setattr("tools.async_delegation.complete_event_delivery", lambda evt, claim: None)

    registry.completion_queue.put(event)
    drained = registry.drain_notifications(session_key="tui-session", skip_poll_observed=False)
    server._notif_handle_ready(
        "tui-sid", session, [event for event, _text in drained], set(), registry,
        format_process_notification, None, owned=True,
    )

    assert any("3" in payload["text"] and "make test" in payload["text"] for _method, _sid, payload in status)
    assert submitted
    assert process.delivered_path
    assert Path(process.delivered_path).exists()


def test_refused_survivor_poller_is_bounded_without_blocking_other_events(tmp_path, monkeypatch):
    registry, process, event = _survivor_registry(tmp_path, monkeypatch)
    session = {"history_lock": threading.RLock(), "running": False, "history": [], "session_key": "tui-session"}
    stop = threading.Event()
    first_attempt = threading.Event()
    unrelated_seen = threading.Event()
    attempts = []
    statuses = []

    def submit(*args, **kwargs):
        attempts.append(args)
        first_attempt.set()
        server._notif_release_turn(session)
        return False

    def emit(method, sid, payload=None):
        if method != "status.update":
            return
        statuses.append(payload)
        if "unrelated output" in payload["text"]:
            unrelated_seen.set()

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    monkeypatch.setattr(server, "_emit", emit)
    monkeypatch.setattr(server, "_background_notifications_off", lambda _: False)
    for name in ("_poll_bot_live_delivery_guarded", "_maybe_fire_tui_loop_tick",
                 "_maybe_fire_tui_heartbeat_tick", "_maybe_resume_tui_parked_goal", "_notif_poll_kanban"):
        monkeypatch.setattr(server, name, lambda *args: None)
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr("tools.async_delegation.maybe_sweep_orphaned_completions", lambda queue: None)
    registry.completion_queue.put(event)
    worker = threading.Thread(target=server._notification_poller_scoped_loop, args=(stop, "tui-sid", session), daemon=True)
    worker.start()
    try:
        assert first_attempt.wait(5), "poller never attempted admission"
        registry.completion_queue.put(dict(event, type="watch_match", output="unrelated output", pattern="unrelated"))
        assert unrelated_seen.wait(2), "parked survivor blocked another queued event"
        stop.wait(1.0)
    finally:
        stop.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    print(f"refused poller: {len(attempts)} submit attempts, {len(statuses)} status emits")
    assert 1 <= len(attempts) <= 5
    assert len(statuses) <= 5
    assert registry._survivor_delivery_pending(process)
    assert registry._survivor_delivery_state(process) is None
    assert registry.completion_queue.qsize() == 1


def test_poller_path_acks_survivor_after_status_emission(tmp_path, monkeypatch):
    registry, process, event = _survivor_registry(tmp_path, monkeypatch)
    session = {"history_lock": threading.RLock(), "running": False, "history": [], "session_key": "tui-session"}
    status = []
    stop = threading.Event()

    def emit(method, sid, payload):
        assert json.loads(Path(process.delivered_path).read_text(encoding="utf-8-sig"))["state"] == "claimed"
        status.append(payload)
        stop.set()

    monkeypatch.setattr(server, "_emit", emit)
    monkeypatch.setattr(server, "_notif_claim_turn", lambda current: True)
    monkeypatch.setattr(server, "_notif_submit", lambda *args, **kwargs: None)
    for name in ("_poll_bot_live_delivery_guarded", "_maybe_fire_tui_loop_tick",
                 "_maybe_fire_tui_heartbeat_tick", "_maybe_resume_tui_parked_goal", "_notif_poll_kanban"):
        monkeypatch.setattr(server, name, lambda *args: None)
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr("tools.async_delegation.maybe_sweep_orphaned_completions", lambda queue: None)
    monkeypatch.setattr("tools.async_delegation.claim_event_delivery", lambda evt, consumer: "claim")
    monkeypatch.setattr("tools.async_delegation.complete_event_delivery", lambda evt, claim: None)
    registry.completion_queue.put(event)
    worker = threading.Thread(target=server._notification_poller_scoped_loop, args=(stop, "tui-sid", session), daemon=True)
    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert any("3" in payload["text"] and "make test" in payload["text"] for payload in status)
    assert Path(process.delivered_path).exists()


def test_notifications_off_mutes_repeated_dedup_key(tmp_path, monkeypatch):
    """`background_process_notifications: off` mutes every offer, not only a first-seen dedup key."""
    registry, _process, event = _survivor_registry(tmp_path, monkeypatch)
    session = {"history_lock": threading.RLock(), "running": False, "history": [], "session_key": "tui-session"}
    dispatched = []
    monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_background_notifications_off", lambda _: True)
    monkeypatch.setattr(server, "_notif_claim_turn", lambda current: True)
    monkeypatch.setattr(server, "_notif_dispatch_event", lambda *args: dispatched.append(args) or True)
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    match = dict(event, type="watch_match", pattern="FAIL", output="FAIL: same line")
    emitted = set()
    for _ in range(2):
        assert server._notif_handle_event("tui-sid", session, dict(match), emitted, registry,
                                          lambda evt: "watch matched", None)
    assert dispatched == []
