"""Known text-send failure has bounded retries and survives for the next owner."""

import contextlib
import json
from unittest.mock import AsyncMock

import pytest

import tools.process_registry as registry_module
from gateway.platforms.base import SendResult
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.mark.asyncio
async def test_text_survivor_failed_sends_back_off_and_leave_pending(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(registry_module, "get_hermes_home", lambda: home)
    run_dir = home / "process-runs" / "proc_failed_text"
    run_dir.mkdir(parents=True)
    (run_dir / "out.log").write_text("finished\n", encoding="utf-8")
    (run_dir / "exit").write_text("0\n", encoding="utf-8")
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_failed_text", command="echo finished", task_id="task", session_key="session",
        survive_restart=True, exited=True, exit_code=0, watcher_platform="telegram",
        watcher_chat_id="123", watcher_interval=0.01, log_path=str(run_dir / "out.log"),
        exit_path=str(run_dir / "exit"), delivered_path=str(run_dir / "delivered"),
    )
    registry._finished[session.id] = session
    registry._write_checkpoint()
    monkeypatch.setattr(registry_module, "process_registry", registry)
    runner, adapter = make_restart_runner()
    runner._completion_event_scope = lambda _: contextlib.nullcontext()
    runner._load_background_notifications_mode = lambda: "concise"
    runner._build_process_event_source = lambda _: make_restart_source(chat_id="123")
    runner._resolve_injection_adapter = lambda *_: adapter
    delays = []
    claims = []

    async def sleep(delay):
        delays.append(delay)
        assert len(delays) <= 5, "text watcher retries without a bound"

    async def send(*args, **kwargs):
        claims.append(registry._survivor_delivery_state(session))
        assert len(claims) <= 5, "text watcher retries without a bound"
        return SendResult(success=False)

    monkeypatch.setattr("gateway.run_notifications.asyncio.sleep", sleep)
    adapter.send = send
    await runner._run_process_watcher({
        "session_id": session.id, "check_interval": 0.01, "platform": "telegram", "chat_id": "123",
    })
    assert claims == ["claimed"] * 5
    assert delays == [0.01, 1.0, 2.0, 4.0, 8.0]
    assert registry._survivor_delivery_state(session) is None
    assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig"))[0]["session_id"] == session.id

    successor = ProcessRegistry()
    assert successor.recover_from_checkpoint() == 1
    assert len(successor.pending_watchers) == 1
    monkeypatch.setattr(registry_module, "process_registry", successor)
    delays.clear()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="delivered"))
    await runner._run_process_watcher(successor.pending_watchers[0])
    adapter.send.assert_awaited_once()
    assert successor._survivor_delivery_state(successor.get(session.id)) == "delivered"
    assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig")) == []
