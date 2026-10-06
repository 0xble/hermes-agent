"""Autonomous wake pacing through the gateway's production seams.

Two load controls, each driven through the same entry points production uses:

- Goal continuation gap: ``_run_post_turn_hooks`` (the hook ``_handle_message`` runs after every
  turn) decides from the event whether the turn carried fresh evidence. A goal continuation or
  ``/loop`` tick is paced; a process/delegation result injected by the completion path pierces the
  hold. The idle ``_loop_wakeup_watcher`` scan then releases the hold silently.
- Completion fan-in: routine results for a busy or recently woken conversation are held and
  delivered as one synthetic turn through the real watcher/injection path. Failures and results a
  parked goal waits on are prompt and carry held siblings with them; shutdown never drops a held
  result.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, build_session_key
from hermes_cli import goals

ROUTE = {
    "session_key": "agent:main:telegram:dm:123", "platform": "telegram",
    "chat_type": "dm", "chat_id": "123", "parent_session_id": "",
}


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import tools.process_registry as pr_module

    monkeypatch.setattr(pr_module, "CHECKPOINT_PATH", home / "processes.json")
    monkeypatch.setattr(pr_module, "process_registry", pr_module.ProcessRegistry())
    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield home
    goals._DB_CACHE.clear()


# ── Goal continuation gap ──────────────────────────────────────────────


class _GoalAdapter:
    def __init__(self) -> None:
        self._pending_messages: dict = {}
        self._active_sessions: dict = {}
        self._message_handler = object()
        self.sends: list[str] = []
        self.handled: list = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sends.append(content)
        return SimpleNamespace(success=True, message_id="m")

    async def handle_message(self, event):
        event._gateway_accepted = True
        self.handled.append(event)


def _goal_runner():
    src = SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="t", chat_type="dm")
    entry = SessionEntry(
        session_key=build_session_key(src), session_id="pace-session", created_at=datetime.now(),
        updated_at=datetime.now(), platform=Platform.TELEGRAM, chat_type="dm", origin=src,
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")})
    runner.adapters = {}
    runner._running = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._queued_events = {}
    adapter = _GoalAdapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store._generate_session_key.return_value = entry.session_key
    runner.session_store.lookup_by_session_id = lambda sid: entry if sid == entry.session_id else None
    runner._restored_source = lambda e: e.origin
    runner._is_session_running = lambda key: False
    runner._queue_depth = lambda key, adapter=None: 0
    runner._goal_min_continuation_gap_from_config = lambda: 900.0

    async def _in_executor(fn, *args):
        return fn(*args)

    runner._run_in_executor_with_context = _in_executor
    return runner, adapter, entry, src


def _turn_event(src, text, *, internal, origin=None):
    metadata = {"notification_origin": origin} if origin else {}
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=src, internal=internal, metadata=metadata)


def _queued_continuations(adapter) -> int:
    return sum(1 for e in adapter._pending_messages.values() if e.text.startswith("[Continuing toward your standing goal]"))


async def _after_turn(runner, src, text, **event_kwargs):
    await runner._run_post_turn_hooks(
        agent_result={"final_response": "did some work"}, source=src,
        is_internal=event_kwargs.get("internal", False), event=_turn_event(src, text, **event_kwargs),
    )


@pytest.mark.asyncio
async def test_post_turn_paces_autonomous_turns_and_lets_results_through(hermes_home, monkeypatch):
    runner, adapter, entry, src = _goal_runner()
    clock = [10_000.0]
    monkeypatch.setattr(goals.time, "time", lambda: clock[0])
    mgr = goals.GoalManager(entry.session_id)
    mgr.set("ship the release")
    continuation = mgr.next_continuation_prompt()

    with patch.object(goals, "judge_goal", return_value=("continue", "more work", False, None, False)):
        # User turn → continuation queued at once.
        await _after_turn(runner, src, "please ship it", internal=False)
        assert _queued_continuations(adapter) == 1
        adapter._pending_messages.clear()

        # The continuation turn itself is autonomous → held, silently, for the gap.
        clock[0] += 20
        await _after_turn(runner, src, continuation, internal=False)
        assert _queued_continuations(adapter) == 0
        assert goals.load_goal(entry.session_id).waiting_until == pytest.approx(10_000.0 + 900)

        # A /loop tick (internal, no completion origin) is paced too.
        clock[0] += 20
        await _after_turn(runner, src, "loop tick", internal=True)
        assert _queued_continuations(adapter) == 0

        # A completion result injected by the process path is fresh evidence: continue now.
        clock[0] += 20
        await _after_turn(runner, src, "[proc done]", internal=True, origin="process_registry_synthetic")
        assert _queued_continuations(adapter) == 1
        assert goals.load_goal(entry.session_id).waiting_until == 0
        adapter._pending_messages.clear()

        # Paced again after that continuation...
        clock[0] += 20
        await _after_turn(runner, src, continuation, internal=False)
        assert _queued_continuations(adapter) == 0
        # ...but a completion drained behind a continuation head (the follow-up chain copies its
        # origin onto the head event, which is not internal) still counts as fresh evidence.
        clock[0] += 20
        await _after_turn(runner, src, continuation, internal=False, origin="process_registry_synthetic")
        assert _queued_continuations(adapter) == 1

    # Nothing about the routine pacing hold reached the chat; only the first continue notice.
    await asyncio.sleep(0)
    assert not any("parked" in s or "wait ended" in s for s in adapter.sends)


@pytest.mark.asyncio
async def test_idle_ticker_releases_gap_hold_once_and_silently(hermes_home, monkeypatch):
    runner, adapter, entry, src = _goal_runner()
    clock = [20_000.0]
    monkeypatch.setattr(goals.time, "time", lambda: clock[0])
    mgr = goals.GoalManager(entry.session_id, min_continuation_gap_seconds=900)
    mgr.set("ship the release")
    with patch.object(goals, "judge_goal", return_value=("continue", "more work", False, None, False)):
        mgr.evaluate_after_turn("first", user_initiated=True)
        clock[0] += 30
        mgr.evaluate_after_turn("second", user_initiated=False)

    await runner._goal_wakeup_fire_one(entry.session_id)
    assert adapter.handled == []  # still inside the gap

    clock[0] = 20_000.0 + 901
    await runner._goal_wakeup_fire_one(entry.session_id)
    assert len(adapter.handled) == 1 and adapter.handled[0].text.startswith("[Continuing toward your standing goal]")
    state = goals.load_goal(entry.session_id)
    assert state.waiting_until == 0 and state.last_continuation_at == pytest.approx(clock[0])
    assert adapter.sends == []  # no "wait ended" notice for routine pacing

    await runner._goal_wakeup_fire_one(entry.session_id)
    assert len(adapter.handled) == 1


def test_gateway_reads_gap_from_config_with_zero_and_default(hermes_home):
    runner, *_ = _goal_runner()
    del runner._goal_min_continuation_gap_from_config
    runner.config = {"goals": {"min_continuation_gap_seconds": 0}}
    assert runner._goal_min_continuation_gap_from_config() == 0
    runner.config = {"goals": {"max_turns": 5}}
    assert runner._goal_min_continuation_gap_from_config() == 900
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["goals"]["min_continuation_gap_seconds"] == 900
    assert DEFAULT_CONFIG["gateway"]["completion_notification_batch_window_seconds"] == 300


# ── Completion fan-in ──────────────────────────────────────────────────


class AdmittingHandler(AsyncMock):
    async def _execute_mock_call(self, event, *args, **kwargs):
        result = await super()._execute_mock_call(event, *args, **kwargs)
        event._gateway_accepted = True
        return result


def _fan_in_runner(*, window=300.0, last_turn_age=10.0):
    adapter = SimpleNamespace(handle_message=AdmittingHandler(), _active_sessions={})
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.session_store = SimpleNamespace(_ensure_loaded=lambda: None, _entries={})
    runner._session_source_cache = {}
    runner._completion_delivery_lock = threading.Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 2048
    runner._background_tasks = set()
    runner._completion_notification_batch_window = window
    runner._session_db = SimpleNamespace(get_session=AsyncMock(return_value={"ended_at": None}))
    if last_turn_age is not None:
        runner._session_state(ROUTE["session_key"]).conversation.last_turn_started_at = time.time() - last_turn_age
    return runner, adapter


def _completion(session_id, *, exit_code=0, started_at=1.0):
    return {
        "type": "completion", **ROUTE, "session_id": session_id, "started_at": started_at,
        "command": f"run {session_id}", "exit_code": exit_code, "completion_reason": "exited",
        "output": f"{session_id} output\n",
    }


def _persist_pending(event):
    from tools import async_delegation

    async_delegation._persist_dispatch({
        "delegation_id": event["delegation_id"], "session_key": event["session_key"],
        "origin_ui_session_id": "", "parent_session_id": event.get("parent_session_id"),
        "dispatched_at": event["dispatched_at"],
    })
    async_delegation._persist_completion(event, {"status": event["status"], "summary": event["summary"]})
    return event


def _delegation(delegation_id, *, status="completed"):
    return {
        "type": "async_delegation", "delegation_id": delegation_id, "session_key": ROUTE["session_key"],
        "goal": "investigate", "status": status, "summary": "found it", "api_calls": 1,
        "duration_seconds": 1.0, "dispatched_at": 1.0, "completed_at": 2.0,
    }


async def _settle(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_idle_session_completion_is_prompt(hermes_home):
    runner, adapter = _fan_in_runner(last_turn_age=10_000)
    assert await runner._enqueue_process_completion_notification("done", _completion("proc_a")) is True
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_recent_session_holds_successes_then_one_turn_lists_all(hermes_home):
    runner, adapter = _fan_in_runner(window=0.4, last_turn_age=0.0)
    results = await asyncio.gather(*(
        runner._enqueue_process_completion_notification(f"text {i}", _completion(f"proc_{i}", started_at=float(i)))
        for i in range(3)
    ))
    assert results == [True, True, True]
    adapter.handle_message.assert_awaited_once()
    text = adapter.handle_message.await_args.args[0].text
    assert "3 background processes completed" in text
    assert all(f"proc_{i}" in text for i in range(3))


@pytest.mark.asyncio
async def test_failure_is_prompt_and_carries_held_successes_in_the_same_turn(hermes_home):
    runner, adapter = _fan_in_runner(window=3600, last_turn_age=0.0)
    held = asyncio.create_task(runner._enqueue_process_completion_notification("ok", _completion("proc_ok")))
    await asyncio.sleep(0.05)
    adapter.handle_message.assert_not_awaited()

    failed = await asyncio.wait_for(
        runner._enqueue_process_completion_notification("bad", _completion("proc_bad", exit_code=1, started_at=2.0)),
        timeout=2.0,
    )
    assert failed is True and await asyncio.wait_for(held, timeout=1.0) is True
    adapter.handle_message.assert_awaited_once()
    text = adapter.handle_message.await_args.args[0].text
    assert "proc_ok" in text and "proc_bad" in text and "exit_code=1" in text


@pytest.mark.asyncio
async def test_result_a_parked_goal_waits_on_is_prompt(hermes_home):
    runner, adapter = _fan_in_runner(window=3600, last_turn_age=0.0)
    mgr = goals.GoalManager("parent-session")
    mgr.set("ship it")
    with patch.object(goals, "_pid_alive", return_value=True):
        mgr._park("waiting for the build", waiting_on_session="proc_build")
    evt = {**_completion("proc_build"), "parent_session_id": "parent-session"}
    assert await asyncio.wait_for(runner._enqueue_process_completion_notification("done", evt), timeout=2.0) is True
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_window_zero_keeps_only_same_tick_fan_in(hermes_home):
    runner, adapter = _fan_in_runner(window=0, last_turn_age=0.0)
    assert await asyncio.wait_for(
        runner._enqueue_process_completion_notification("done", _completion("proc_a")), timeout=1.0) is True
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_watcher_holds_routine_delegation_results_and_delivers_one_turn(hermes_home, monkeypatch):
    import tools.process_registry as pr_module

    q = queue.Queue()
    monkeypatch.setattr(pr_module.process_registry, "completion_queue", q)
    runner, adapter = _fan_in_runner(window=1.0, last_turn_age=None)

    async def _drive():
        task = asyncio.create_task(runner._async_delegation_watcher(interval=0.05))
        await asyncio.sleep(3.1)  # watcher's startup delay is 3s
        # A turn just started; two routine results land a few watcher ticks apart.
        runner._session_state(ROUTE["session_key"]).conversation.last_turn_started_at = time.time()
        q.put(_persist_pending(_delegation("deleg_a")))
        await asyncio.sleep(0.3)
        assert adapter.handle_message.await_count == 0
        q.put(_persist_pending(_delegation("deleg_b")))
        await _settle(lambda: adapter.handle_message.await_count >= 1, timeout=3.0)
        runner._running = False
        await asyncio.wait_for(task, timeout=2.0)

    await _drive()
    adapter.handle_message.assert_awaited_once()
    text = adapter.handle_message.await_args.args[0].text
    assert "2 background subagent delegations" in text and "deleg_a" in text and "deleg_b" in text
    from tools import async_delegation

    for delegation_id in ("deleg_a", "deleg_b"):
        assert async_delegation.get_durable_delegation(delegation_id)["delivery_state"] == "delivered"


@pytest.mark.asyncio
async def test_failed_delegation_is_prompt(hermes_home):
    runner, adapter = _fan_in_runner(window=3600, last_turn_age=0.0)
    assert await asyncio.wait_for(
        runner._enqueue_async_delegation_group([_delegation("deleg_bad", status="error")]), timeout=2.0) is True
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_releases_held_completion_instead_of_dropping_it(hermes_home):
    runner, adapter = _fan_in_runner(window=3600, last_turn_age=0.0)
    held = asyncio.create_task(runner._enqueue_process_completion_notification("ok", _completion("proc_held")))
    await asyncio.sleep(0.05)
    adapter.handle_message.assert_not_awaited()

    await asyncio.wait_for(runner._cancel_process_completion_batch_tasks(), timeout=7.0)

    assert await asyncio.wait_for(held, timeout=1.0) is True
    adapter.handle_message.assert_awaited_once()
    assert runner._completion_notification_batches == {}
    assert runner._completion_notification_batch_flush_tasks == set()


@pytest.mark.asyncio
async def test_shutdown_requeues_held_delegations(hermes_home, monkeypatch):
    import tools.process_registry as pr_module

    q = queue.Queue()
    monkeypatch.setattr(pr_module.process_registry, "completion_queue", q)
    runner, adapter = _fan_in_runner(window=3600, last_turn_age=0.0)
    runner._deliver_async_delegation_group = AsyncMock(return_value=False)
    assert await runner._enqueue_async_delegation_group([_delegation("deleg_held")]) is True

    await asyncio.wait_for(runner._cancel_process_completion_batch_tasks(), timeout=7.0)

    runner._deliver_async_delegation_group.assert_awaited_once()
    assert [evt["delegation_id"] for evt in list(q.queue)] == ["deleg_held"]


@pytest.mark.asyncio
async def test_task_failure_notice_releases_held_delegation_results_now(hermes_home):
    """The diagnostic failure lane keeps its own turn (and mute policy) but never leaves held
    sibling results waiting out the window."""
    runner, adapter = _fan_in_runner(window=3600, last_turn_age=0.0)
    delivered: list = []

    async def _deliver(group):
        delivered.append([evt["delegation_id"] for evt in group])
        return True

    runner._deliver_async_delegation_group = _deliver
    assert await runner._enqueue_async_delegation_group([_delegation("deleg_ok")]) is True
    await asyncio.sleep(0.2)
    assert delivered == []

    notice = {**_delegation("deleg_batch"), "task_failure_notice": True, "status": "running",
              "results": [{"task_index": 0, "status": "failed"}]}
    assert await asyncio.wait_for(runner._enqueue_async_delegation_group([notice]), timeout=1.0) is True
    await _settle(lambda: len(delivered) == 2, timeout=1.0)
    assert sorted(delivered) == [["deleg_batch"], ["deleg_ok"]]


@pytest.mark.asyncio
async def test_completion_of_goal_awaited_pid_is_prompt(hermes_home):
    import tools.process_registry as pr_module
    from tools.process_registry import ProcessSession

    runner, adapter = _fan_in_runner(window=3600, last_turn_age=0.0)
    session = ProcessSession(id="proc_pid", command="build", task_id="t", started_at=1.0,
                             exited=True, exit_code=0, pid=43210)
    pr_module.process_registry._finished[session.id] = session
    mgr = goals.GoalManager("parent-session")
    mgr.set("ship it")
    with patch.object(goals, "_pid_alive", return_value=True):
        mgr.wait_on(43210, reason="waiting for the build pid")
    evt = {**_completion("proc_pid"), "parent_session_id": "parent-session"}
    assert await asyncio.wait_for(runner._enqueue_process_completion_notification("done", evt), timeout=2.0) is True
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_busy_turn_receipt_is_sent_while_the_wake_is_held(hermes_home, monkeypatch):
    """#112033: a process finishing during its launching turn gets the chat receipt immediately,
    even though the agent wake is held for fan-in."""
    import tools.process_registry as pr_module
    from tools.process_registry import ProcessSession

    runner, adapter = _fan_in_runner(window=3600, last_turn_age=0.0)
    adapter._active_sessions[ROUTE["session_key"]] = asyncio.Event()  # launching turn still running
    sends: list[str] = []

    async def _send(chat_id, text, **_kwargs):
        sends.append(text)
        return SimpleNamespace(success=True)

    adapter.send = _send
    session = ProcessSession(id="proc_busy", command="make test", task_id="t", started_at=1.0,
                             output_buffer="ok\n", exited=True, exit_code=0, notify_on_complete=True)
    pr_module.process_registry._finished[session.id] = session
    watcher = {"session_id": "proc_busy", "check_interval": 0, **ROUTE, "notify_on_complete": True}
    task = asyncio.create_task(runner._run_process_watcher(watcher))
    await _settle(lambda: bool(sends), timeout=2.0)
    assert len(sends) == 1
    await _settle(lambda: bool(runner._completion_notification_batches), timeout=2.0)
    await asyncio.sleep(0.2)
    adapter.handle_message.assert_not_awaited()  # the wake itself is still held
    assert not task.done()
    await runner._cancel_process_completion_batch_tasks()
    await asyncio.wait_for(task, timeout=2.0)
    adapter.handle_message.assert_awaited_once()
    assert len(sends) == 1
