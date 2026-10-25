"""Late checkpoint adoption must deliver without an unrelated foreground turn."""

import asyncio
import contextlib
import contextvars
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import tools.process_registry as registry_module
from gateway.run import _profile_runtime_scope
from gateway.zero_cut_standby import _adopt_predecessor_duties
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.mark.asyncio
@pytest.mark.parametrize("secondary", [False, True], ids=["launch-profile", "secondary-profile"])
async def test_late_adoption_arms_recovered_watcher_without_user_turn(tmp_path, monkeypatch, secondary):
    launch_home = tmp_path / "home"
    home = launch_home / "profiles" / "satellite" if secondary else launch_home
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    run_dir = home / "process-runs" / "proc_adopted"
    run_dir.mkdir(parents=True)
    (run_dir / "out.log").write_text("original completion\n", encoding="utf-8")
    (run_dir / "exit").write_text("0\n", encoding="utf-8")
    producer = ProcessRegistry()
    original = ProcessSession(
        id="proc_adopted", command="completed original command", task_id="task", session_key="session",
        survive_restart=True, notify_on_complete=True, parent_session_id="original-parent",
        watcher_platform="telegram", watcher_chat_id="123", watcher_thread_id="7",
        watcher_interval=1,
        log_path=str(run_dir / "out.log"), exit_path=str(run_dir / "exit"),
        delivered_path=str(run_dir / "delivered"),
    )
    producer._running[original.id] = original
    with _profile_runtime_scope(home, {}):
        producer._write_checkpoint()
    successor = ProcessRegistry()
    monkeypatch.setattr(registry_module, "process_registry", successor)
    runner, adapter = make_restart_runner()
    runner.config.multiplex_profiles = secondary
    runner.config.durable_outbox_enabled = False
    monkeypatch.setattr("gateway.run._multiplex_profile_homes", lambda _: [("satellite", home)])
    monkeypatch.setattr("gateway.gateway_slot.slot_lock_held", lambda *_: False)
    monkeypatch.setattr("gateway.status._get_process_hermes_home", lambda: launch_home)
    monkeypatch.setattr("gateway.run._claim_host_gateway_role", lambda: None)
    monkeypatch.setattr("gateway.run._refresh_host_gateway_record", lambda _: None)
    runner._completion_event_scope = lambda _: contextlib.nullcontext()
    runner._load_background_notifications_mode = lambda: "concise"
    runner._build_process_event_source = lambda _: make_restart_source(chat_id="123", thread_id="7")
    runner._resolve_injection_adapter = lambda *_: adapter
    runner._enqueue_process_completion_notification = AsyncMock(return_value=True)
    try:
        # Startup has already drained an empty batch. Recovery happens only now.
        assert successor.pending_watchers == []
        await _adopt_predecessor_duties(runner)
        assert successor.pending_watchers == [], (
            "Late adoption must arm its recovered watcher without a user turn"
        )
        tasks = tuple(runner._background_tasks)
        assert tasks, "Late adoption must retain its completion task"
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        runner._enqueue_process_completion_notification.assert_awaited_once()
        event = runner._enqueue_process_completion_notification.await_args.args[1]
        assert event["session_id"] == original.id
        assert event["parent_session_id"] == "original-parent"
        assert event["chat_id"] == "123" and event["thread_id"] == "7"
        assert event["exit_code"] == 0 and event["output"].count("original completion") == 1
        assert json.loads((run_dir / "delivered").read_text(encoding="utf-8-sig"))["delivered"] is True
        # A repeated adoption cannot arm a second watcher or repeat the notification.
        await _adopt_predecessor_duties(runner)
        assert successor.pending_watchers == []
        assert not runner._background_tasks
        runner._enqueue_process_completion_notification.assert_awaited_once()
    finally:
        runner._running = False
        tasks = tuple(runner._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        successor.stop_survivor_pollers()


@pytest.mark.asyncio
async def test_shared_watcher_drain_keeps_concurrent_appends_and_fresh_context():
    runner, _ = make_restart_runner()
    marker = contextvars.ContextVar("watcher_origin", default="root")
    token = marker.set("delegated")
    registry = SimpleNamespace(pending_watchers=[{"session_id": str(i)} for i in range(101)])
    started = asyncio.Queue()
    release = asyncio.Event()

    async def watch(descriptor):
        assert marker.get() == "root", "A parent watcher must not inherit delegated context"
        if descriptor["session_id"] == "0":
            registry.pending_watchers.append({"session_id": "late"})
        started.put_nowait(descriptor["session_id"])
        await release.wait()

    runner._run_process_watcher = watch
    try:
        await runner._start_pending_process_watchers(registry)
        assert registry.pending_watchers == [{"session_id": "late"}]
        assert len(runner._background_tasks) == 101
        await runner._start_pending_process_watchers(registry)
        assert registry.pending_watchers == []
        assert len(runner._background_tasks) == 102
        release.set()
        await asyncio.wait_for(asyncio.gather(*runner._background_tasks), timeout=5)
        observed = [started.get_nowait() for _ in range(started.qsize())]
        assert len(observed) == len(set(observed)) == 102
        assert set(observed) == {str(i) for i in range(101)} | {"late"}
    finally:
        marker.reset(token)
        for task in tuple(runner._background_tasks):
            task.cancel()
        await asyncio.gather(*runner._background_tasks, return_exceptions=True)
