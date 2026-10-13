"""Real TUI consumer coverage for restart-survivor completion delivery."""

import json
import threading
from pathlib import Path

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
