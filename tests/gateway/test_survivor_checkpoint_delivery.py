"""Recovery must preserve gateway delivery of file-backed results without a live PID."""

import contextlib
import json
from unittest.mock import AsyncMock

import pytest

import tools.process_registry as registry_module
from gateway.platforms.base import SendResult
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.mark.asyncio
async def test_recycled_pid_recorded_exit_restores_gateway_watcher(tmp_path, monkeypatch):
    import asyncio

    home = tmp_path / "home"
    monkeypatch.setattr(registry_module, "get_hermes_home", lambda: home)
    run_dir = home / "process-runs" / "proc_recycled"
    run_dir.mkdir(parents=True)
    (run_dir / "out.log").write_text("finished\n", encoding="utf-8")
    (run_dir / "exit").write_text("4\n", encoding="utf-8")
    producer = ProcessRegistry()
    session = ProcessSession(
        id="proc_recycled", command="echo finished", pid=999999, task_id="task", session_key="session",
        survive_restart=True, notify_on_complete=True, watcher_platform="telegram", watcher_chat_id="123",
        watcher_thread_id="7", watcher_interval=0.01, log_path=str(run_dir / "out.log"),
        exit_path=str(run_dir / "exit"), delivered_path=str(run_dir / "delivered"),
    )
    producer._running[session.id] = session
    producer._write_checkpoint()
    successor = ProcessRegistry()
    monkeypatch.setattr(successor, "_detached_host_fate", lambda *_: "reused")
    assert successor.recover_from_checkpoint() == 1
    assert successor.completion_queue.qsize() == 1
    monkeypatch.setattr(registry_module, "process_registry", successor)
    watchers = successor.pending_watchers
    assert len(watchers) == 1
    assert watchers[0]["chat_id"] == "123" and watchers[0]["thread_id"] == "7"
    runner, adapter = make_restart_runner()
    runner._completion_event_scope = lambda _: contextlib.nullcontext()
    runner._load_background_notifications_mode = lambda: "concise"
    runner._build_process_event_source = lambda _: make_restart_source(chat_id="123", thread_id="7")
    runner._resolve_injection_adapter = lambda *_: adapter
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m"))
    # Keep the real watcher receipt send; only the model-turn admission is a stub.
    adapter._active_sessions["session"] = asyncio.Event()
    runner._enqueue_process_completion_notification = AsyncMock(return_value=True)
    await asyncio.wait_for(runner._run_process_watcher(watchers[0]), timeout=5)
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[0] == "123"
    assert adapter.send.await_args.kwargs["metadata"]["thread_id"] == "7"
    runner._enqueue_process_completion_notification.assert_awaited_once()
    assert runner._enqueue_process_completion_notification.await_args.args[1]["exit_code"] == 4
    assert json.loads((run_dir / "delivered").read_text(encoding="utf-8-sig"))["delivered"] is True
