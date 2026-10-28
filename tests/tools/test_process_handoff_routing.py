"""Explicit process handoff transfers completion, not just teardown ownership.

Regression for the handoff gap documented in upstream #131578. Models and the
transport are controlled, but tool dispatch, processes, child teardown, routing,
checkpoint I/O and gateway admission use their real implementations.
"""

import asyncio
import json
import shlex
import sys
import threading
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.session_context import scoped_current_session_id, set_session_vars, clear_session_vars
from tests.waiting import async_wait_until
from tools import delegate_tool, process_registry as processes
from tools.process_registry_wait import completion_reaches_session
from tools.registry import registry as tools
from tools.terminal_tool import terminal_tool  # imported to register the real terminal handler


def _call(name, args, owner):
    payload = tools.dispatch(name, args, task_id=owner)
    assert isinstance(payload, str)
    result = json.loads(payload)
    assert not result.get("error"), result
    return result


def _command(tmp_path):
    gate = tmp_path / "release"
    script = tmp_path / "worker.py"
    script.write_text(
        "import pathlib, sys, threading, time\n"
        "gate = pathlib.Path(sys.argv[1])\n"
        "deadline = time.monotonic() + 30\n"
        "while not gate.exists():\n"
        "    if time.monotonic() >= deadline: raise SystemExit(9)\n"
        "    threading.Event().wait(0.01)\n"
        "print('HANDOFF_RESULT', flush=True)\n",
        encoding="utf-8",
    )
    return gate, shlex.join([sys.executable, str(script), str(gate)])


class _Parent:
    def __init__(self, session_id, owner="parent-turn"):
        self.session_id = session_id
        self._current_task_id = owner
        self._active_children = []
        self._active_children_lock = threading.Lock()
        self._interrupt_requested = False


class _Child:
    def __init__(self, parent, command, notify, first_poll=None):
        self.session_id = "child-durable-session"
        self._parent_session_id = parent.session_id
        self._delegate_parent_ref = weakref.ref(parent)
        self._subagent_id = "sa-0-routing"
        self._delegate_depth = 1
        self._delegate_role = "leaf"
        self.tool_progress_callback = None
        self._credential_pool = None
        self.command, self.notify, self.first_poll = command, notify, first_poll
        self.proc = None
        self.closed = False

    def run_conversation(self, *, task_id, **kwargs):
        with scoped_current_session_id(self.session_id):
            spawned = _call("terminal", {
                "command": self.command, "background": True, "notify_on_complete": self.notify,
            }, task_id)
            self.proc = processes.process_registry.get(spawned["session_id"])
            assert self.proc is not None
            if self.first_poll is not None and self.notify:
                assert self.first_poll.wait(10), "watcher did not start before handoff"
            _call("process_manage", {
                "action": "handoff", "session_id": self.proc.id, "data": "collect worker result",
            }, task_id)
        return {"final_response": "handed off", "completed": True, "api_calls": 0, "messages": []}

    def close(self):
        self.closed = True
        processes.process_registry.kill_all(self._subagent_id, source="subagent_cleanup")


@pytest.fixture
def handoff_runtime(monkeypatch):
    reg = processes.ProcessRegistry()
    monkeypatch.setattr(processes, "process_registry", reg)
    monkeypatch.setattr(processes, "_SYSTEMD_SCOPE_AVAILABLE", False)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 20)
    yield reg
    reg.kill_all(source="test")
    clear_session_vars([])


@pytest.mark.parametrize("notify", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("consume", ["drain", "wait"])
def test_handoff_survives_child_cleanup_and_transfers_completion(
    tmp_path, monkeypatch, handoff_runtime, notify, nested, consume,
):
    import gateway.run as gateway_run
    from tools.process_registry import _checkpoint_path

    reg = handoff_runtime
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: None)
    route = "agent:main:telegram:group:123:42"
    set_session_vars(platform="telegram", chat_id="123", thread_id="42", session_key=route)
    parent = _Parent("receiving-parent", "sa-0-orchestrator" if nested else "parent-turn")
    gate, command = _command(tmp_path)
    child = _Child(parent, command, notify)
    entry = delegate_tool._run_single_child(0, "start worker", child, parent)
    assert entry["status"] == "completed", entry
    proc = child.proc
    assert proc is not None
    assert child.closed and not proc.exited
    assert entry["handed_off_processes"][0]["session_id"] == proc.id
    assert reg.running_owned_by(child._subagent_id) == []
    assert proc.owner_task_id == parent._current_task_id
    assert proc.parent_session_id == parent.session_id
    assert proc.session_key == route and proc.notify_on_complete
    saved = next(row for row in json.loads(_checkpoint_path().read_text()) if row["session_id"] == proc.id)
    assert saved["parent_session_id"] == parent.session_id
    assert saved["session_key"] == route and saved["notify_on_complete"]
    assert saved["owner_task_id"] == parent._current_task_id
    assert saved["handoff_note"] == "collect worker result"
    assert len([w for w in reg.pending_watchers if w["session_id"] == proc.id]) == 1

    if nested:
        from tools.delegate_tool import _register_subagent, _unregister_subagent
        grandparent = _Parent("grandparent-durable")
        parent._delegate_parent_ref = weakref.ref(grandparent)
        _register_subagent({"subagent_id": parent._current_task_id, "agent": parent})
        try:
            with scoped_current_session_id(parent.session_id):
                _call("process_manage", {
                    "action": "handoff", "session_id": proc.id, "data": "collect worker result",
                }, parent._current_task_id)
        finally:
            _unregister_subagent(parent._current_task_id)
        parent = grandparent

    with scoped_current_session_id(parent.session_id):
        if not nested:
            assert completion_reaches_session(proc)
        with scoped_current_session_id("new-user-session"):
            assert not completion_reaches_session(proc)
        gate.touch()
        if consume == "wait":
            result = _call("process_manage", {"action": "wait", "session_id": proc.id, "timeout": 10}, parent._current_task_id)
            assert result["exit_code"] == 0 and "HANDOFF_RESULT" in result["output"]
            assert reg.drain_notifications(owns_event=lambda evt: True) == []
        else:
            assert proc._completion_event.wait(10)
            events = reg.drain_notifications(owns_event=lambda evt: True)
            assert len(events) == 1
            event, text = events[0]
            assert event["parent_session_id"] == parent.session_id
            assert event["session_key"] == route
            assert "HANDOFF_RESULT" in text and "collect worker result" in text
            assert reg.drain_notifications(owns_event=lambda evt: True) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("notify", [False, True])
@pytest.mark.parametrize("new_session", [False, True])
@pytest.mark.parametrize("mode", ["concise", "off"])
async def test_gateway_watcher_follows_handoff_without_child_pin_or_duplicate(
    tmp_path, monkeypatch, handoff_runtime, notify, new_session, mode,
):
    import gateway.run as gateway_run
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    reg = handoff_runtime
    (tmp_path / "config.yaml").write_text(f"display:\n  background_process_notifications: {mode}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    runner._completion_notification_batch_window = 0

    async def admit(event):
        event._gateway_accepted = True

    adapter = SimpleNamespace(send=AsyncMock(), handle_message=AsyncMock(side_effect=admit), _active_sessions={})
    runner.adapters[Platform.TELEGRAM] = adapter
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", thread_id="42", chat_type="group", user_id="u")
    entry = runner.session_store.get_or_create_session(source)
    db = runner.session_store._db_for_key(entry.session_key)
    db.create_session("child-durable-session", source="subagent", parent_session_id=entry.session_id)
    parent = _Parent(entry.session_id)
    gate, command = _command(tmp_path)
    first_poll = threading.Event()
    child = _Child(parent, command, notify, first_poll)
    watchers = []
    runner._gateway_loop = asyncio.get_running_loop()
    runner._running = True
    run_watcher = runner._run_process_watcher

    async def observed_watcher(watcher):
        watchers.append(asyncio.current_task())
        watcher["check_interval"] = 0
        await run_watcher(watcher)

    monkeypatch.setattr(runner, "_run_process_watcher", observed_watcher)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    get = reg.get

    def observed_get(sid):
        proc = get(sid)
        if threading.current_thread().name.startswith("asyncio"):
            first_poll.set()
        return proc

    monkeypatch.setattr(reg, "get", observed_get)
    set_session_vars(platform="telegram", chat_id="123", thread_id="42", session_key=entry.session_key)
    try:
        result = await asyncio.to_thread(delegate_tool._run_single_child, 0, "start worker", child, parent)
        assert result["status"] == "completed", result
        assert child.closed and not child.proc.exited
        if new_session:
            runner.session_store.reset_session(entry.session_key)
        gate.touch()
        assert await asyncio.to_thread(child.proc._completion_event.wait, 10)
        await async_wait_until(lambda: bool(watchers), timeout=10)
        await asyncio.wait_for(asyncio.gather(*watchers), timeout=10)
        assert len(watchers) == 1
        admitted = not new_session and mode != "off"
        assert adapter.handle_message.await_count == int(admitted)
        if admitted:
            event = adapter.handle_message.await_args.args[0]
            assert event.metadata["gateway_session_id"] == parent.session_id
            assert event.metadata["gateway_session_key"] == entry.session_key
            assert "HANDOFF_RESULT" in event.text and "collect worker result" in event.text
            assert runner.session_store.lookup_by_session_key(entry.session_key).session_id == parent.session_id
        assert db.get_session(child.session_id)["source"] == "subagent"
        adapter.send.assert_not_awaited()
        if mode == "off" and not new_session:
            assert child.proc is not None
            with scoped_current_session_id(parent.session_id):
                result = _call("process_manage", {
                    "action": "wait", "session_id": child.proc.id, "timeout": 10,
                }, parent._current_task_id)
            assert result["exit_code"] == 0 and "HANDOFF_RESULT" in result["output"]
    finally:
        gate.touch()
        for watcher in watchers:
            if not watcher.done():
                watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
        db.close()
