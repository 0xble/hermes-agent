"""Known send failures stay retryable in-process and across a gateway restart."""

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock

import pytest

import tools.process_registry as registry_module
from gateway.platforms.base import SendResult
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.mark.asyncio
async def test_watcher_unknown_send_result_is_not_repeated(monkeypatch):
    runner, adapter = make_restart_runner()
    runner._build_process_event_source = lambda _: make_restart_source(chat_id="123")
    runner._resolve_injection_adapter = lambda *_: adapter
    adapter.send = AsyncMock(return_value=None)

    assert await runner._send_watcher_message("telegram", "123", "", "finished", {}) is True
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["send-raises", "adapter-missing"])
async def test_transient_watcher_delivery_failure_retries_until_delivered(tmp_path, monkeypatch, failure):
    """A raised send or a temporarily absent adapter (reconnect, runtime fatal) is never settled."""
    monkeypatch.setattr(registry_module, "get_hermes_home", lambda: tmp_path / "home")
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_transient", command="echo finished", task_id="task", session_key="session",
        survive_restart=True, exited=True, exit_code=0,
        watcher_platform="telegram", watcher_chat_id="123",
        delivered_path=str(tmp_path / "home" / "process-runs" / "proc_transient" / "delivered"),
    )
    registry._finished[session.id] = session
    monkeypatch.setattr(registry_module, "process_registry", registry)
    runner, adapter = make_restart_runner()
    runner._completion_event_scope = lambda _: contextlib.nullcontext()
    runner._load_background_notifications_mode = lambda: "concise"
    runner._build_process_event_source = lambda _: make_restart_source(chat_id="123")
    resolves = []

    def resolve(*_):
        resolves.append(1)
        return None if failure == "adapter-missing" and len(resolves) == 1 else adapter

    runner._resolve_injection_adapter = resolve
    sends = []

    async def send(*args, **kwargs):
        sends.append(registry._survivor_delivery_state(session))
        if failure == "send-raises" and len(sends) == 1:
            raise ConnectionError("network down")
        return SendResult(success=True, message_id="delivered")

    adapter.send = send
    delays = []

    async def sleep(delay):
        delays.append(delay)
        assert registry._survivor_delivery_state(session) != "delivered", "transient failure settled"
        assert len(delays) <= 3

    monkeypatch.setattr("gateway.run_notifications.asyncio.sleep", sleep)
    watcher = {"session_id": session.id, "session_key": session.session_key,
               "check_interval": 0.01, "platform": "telegram", "chat_id": "123"}
    await runner._run_process_watcher(watcher)
    assert sends == (["claimed", "claimed"] if failure == "send-raises" else ["claimed"])
    assert registry._survivor_delivery_state(session) == "delivered"


@pytest.mark.asyncio
async def test_send_exception_reports_known_failure(monkeypatch):
    runner, adapter = make_restart_runner()
    runner._build_process_event_source = lambda _: make_restart_source(chat_id="123")
    runner._resolve_injection_adapter = lambda *_: adapter
    adapter.send = AsyncMock(side_effect=ConnectionError("network down"))

    assert await runner._send_watcher_message("telegram", "123", "", "finished", {}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["in-process", "restart", "unknown-result"])
async def test_text_survivor_failed_sends_back_off_and_leave_pending(tmp_path, monkeypatch, recovery):
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
        assert registry._survivor_delivery_state(session) is None
        assert len(delays) <= 14, "watcher repeated a successful or unknown send"
        if recovery == "restart" and len(delays) == 14:
            raise asyncio.CancelledError

    async def send(*args, **kwargs):
        claims.append(registry._survivor_delivery_state(session))
        assert len(claims) <= 14, "watcher repeated a successful or unknown send"
        if len(claims) <= 13:
            return SendResult(success=False)
        return None if recovery == "unknown-result" else SendResult(success=True, message_id="delivered")

    monkeypatch.setattr("gateway.run_notifications.asyncio.sleep", sleep)
    adapter.send = send
    watcher = {"session_id": session.id, "check_interval": 0.01, "platform": "telegram", "chat_id": "123"}
    if recovery == "restart":
        with pytest.raises(asyncio.CancelledError):
            await runner._run_process_watcher(watcher)
    else:
        await runner._run_process_watcher(watcher)
    assert delays == [0.01, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 900.0, 900.0, 900.0]
    assert claims == ["claimed"] * (13 if recovery == "restart" else 14)
    if recovery != "restart":
        assert registry._survivor_delivery_state(session) == "delivered"
        assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig")) == []
        return
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
