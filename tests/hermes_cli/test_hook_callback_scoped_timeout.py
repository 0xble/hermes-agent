"""Completion-driven hook refusal: no timer, bounded live workers, terminal policy vetoes."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes_cli import plugins, plugins_dispatch as dispatch
from tests.waiting import wait_until


class HookRig:
    """Real manager/threads; manually expire only callbacks held by this test.

    Synchronize the deadline after callback entry, not on scheduler speed. The
    dispatcher's Event.wait is the timeout seam; no clock advance or sleep is used.
    """

    def __init__(self, monkeypatch):
        self.manager = plugins.PluginManager()
        self.manager._discovered = True
        self.manager._discovery_complete = True
        self.holds = {}
        self.held_events = []
        self.verdicts = {}
        self.calls = []
        self.workers = []
        self.clients = []
        self.condition = threading.Condition()
        self.local = threading.local()
        self.deadlines = threading.Event()
        self.deadlines.set()
        self.start_gate = threading.Event()
        self.start_gate.set()
        self.created = 0
        self.start_failure = False
        self.finish_before_expiry = False
        rig = self

        class Done(threading.Event):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.held = False

            def wait(self, timeout=None):
                assert timeout == 0.001
                assert self.entered.wait(5), "callback never entered"
                if not self.held:
                    return super().wait(5)
                assert rig.deadlines.wait(5), "test never released callback deadlines"
                if rig.finish_before_expiry:
                    for hold in rig.holds.values():
                        hold.set()
                    assert super().wait(5)
                    return False  # completion between wait expiry and timeout accounting
                return super().wait(0)

        def event():
            done = Done()
            self.local.done = done
            return done

        def worker(*, target, **kwargs):
            done = self.local.done
            if self.start_failure == "construct":
                raise RuntimeError("cannot construct worker")

            def invoke():
                self.local.done = done
                try:
                    target()
                finally:
                    done.entered.set()  # ordinary, non-held callbacks also reach the wait seam

            with self.condition:
                self.created += 1
                self.condition.notify_all()
            assert self.start_gate.wait(5), "test never released worker construction"
            thread = threading.Thread(target=invoke, **kwargs)
            self.workers.append(thread)
            if self.start_failure == "start":
                thread.start = lambda: (_ for _ in ()).throw(RuntimeError("no worker"))
            return thread

        monkeypatch.setattr(dispatch, "threading", SimpleNamespace(Event=event, Thread=worker))
        monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0.001)
        self.manager._hooks["pre_tool_call"] = [self.callback]
        monkeypatch.setattr(plugins, "_plugin_manager", self.manager)

    def callback(self, **kwargs):
        call = kwargs.get("tool_call_id", "anonymous")
        done = self.local.done
        hold = self.holds.get(call)
        done.held = hold is not None
        with self.condition:
            self.calls.append((call, kwargs.get("session_id"), kwargs.get("tool_name")))
            self.condition.notify_all()
        done.entered.set()
        if hold is not None:
            hold.wait()
        return self.verdicts.get(call)

    def hold(self, *calls):
        for call in calls:
            self.holds[call] = threading.Event()
            self.held_events.append(self.holds[call])

    def fire(self, call, hook="pre_tool_call", **kwargs):
        return self.manager.invoke_hook(hook, tool_call_id=call, **kwargs)

    def client(self, fn):
        result = {}
        finished = threading.Event()

        def invoke():
            try:
                result["value"] = fn()
            except BaseException as exc:
                result["error"] = exc
            finally:
                finished.set()
                with self.condition:
                    self.condition.notify_all()

        thread = threading.Thread(target=invoke, daemon=True)
        self.clients.append(thread)
        thread.start()
        return result, finished

    def drain(self):
        assert wait_until(lambda: not self.manager._hook_running_callbacks, timeout=5)
        assert not self.manager._hook_abandoned
        assert not self.manager._hook_live_workers

    def close(self):
        self.start_gate.set()
        self.deadlines.set()
        for hold in self.held_events:
            hold.set()
        for group in (self.clients, self.workers):
            for thread in group:
                if thread.ident is not None:
                    thread.join(5)
                    assert not thread.is_alive()
        self.drain()


@pytest.fixture
def rig(monkeypatch):
    harness = HookRig(monkeypatch)
    try:
        yield harness
    finally:
        harness.close()


def assert_block(results, reason=None):
    blocks = [r for r in results if isinstance(r, dict) and r.get("action") == "block"]
    assert len(blocks) == 1
    assert blocks[0].get("message", "").strip()
    if reason:
        assert reason in blocks[0]["message"]


def test_completed_timeout_does_not_poison_fresh_call(rig):
    rig.hold("a")
    assert_block(rig.fire("a"))
    rig.holds["a"].set()
    rig.drain()
    rig.verdicts["b"] = {"action": "block", "message": "real guard verdict"}
    assert rig.fire("b") == [rig.verdicts["b"]]
    assert [c[0] for c in rig.calls] == ["a", "b"]


def test_one_abandoned_call_does_not_block_another_session_or_write_evaluation(rig):
    rig.hold("a")
    assert_block(rig.fire("a", session_id="session-a", tool_name="terminal"))
    assert rig.fire("b", session_id="session-b", tool_name="read_file") == []
    rig.verdicts["c"] = {"action": "block", "message": "protected write"}
    assert rig.fire("c", session_id="session-b", tool_name="write_file") == [rig.verdicts["c"]]
    assert len(rig.calls) == 3


def test_two_abandoned_workers_refuse_immediately_and_completion_recovers(rig):
    rig.hold("a", "b")
    assert_block(rig.fire("a"))
    assert_block(rig.fire("b"))
    assert_block(rig.fire("c"), "callback_hung")
    assert [c[0] for c in rig.calls] == ["a", "b"]
    rig.holds["a"].set()
    assert wait_until(lambda: len(rig.manager._hook_running_callbacks) == 1, timeout=5)
    rig.verdicts["d"] = "fresh evaluation"
    assert rig.fire("d") == ["fresh evaluation"]


def test_more_than_threshold_requires_falling_below_threshold(rig):
    rig.deadlines.clear()
    rig.hold("a", "b", "c")
    pending = [rig.client(lambda c=c: rig.fire(c)) for c in ("a", "b", "c")]
    with rig.condition:
        assert rig.condition.wait_for(lambda: len(rig.calls) == 3, timeout=5)
    rig.deadlines.set()
    for result, done in pending:
        assert done.wait(5)
        assert_block(result["value"])
    rig.holds["a"].set()
    assert wait_until(lambda: len(rig.manager._hook_running_callbacks) == 2, timeout=5)
    assert_block(rig.fire("d"), "callback_hung")
    rig.holds["b"].set()
    assert wait_until(lambda: len(rig.manager._hook_running_callbacks) == 1, timeout=5)
    assert rig.fire("e") == []
    assert sorted(c[0] for c in rig.calls) == ["a", "b", "c", "e"]


@pytest.mark.parametrize("reserve_before_start", [False, True])
def test_healthy_parallel_budget_is_atomic_and_separate_from_hung_threshold(rig, reserve_before_start):
    rig.deadlines.clear()
    if reserve_before_start:
        rig.start_gate.clear()
    rig.hold(*(str(i) for i in range(33)))
    barrier = threading.Barrier(33)

    def fire(i):
        barrier.wait(5)
        return rig.fire(str(i))

    pending = [rig.client(lambda i=i: fire(i)) for i in range(32)]
    barrier.wait(5)
    with rig.condition:
        assert rig.condition.wait_for(lambda: rig.created == 32, timeout=5)
        if not reserve_before_start:
            assert rig.condition.wait_for(lambda: len(rig.calls) == 32, timeout=5)
    overflow, done = rig.client(lambda: rig.fire("32"))
    with rig.condition:
        assert rig.condition.wait_for(lambda: done.is_set() or rig.created > 32, timeout=5)
    assert rig.created == 32, "33rd call started a worker despite the live/reserved budget"
    assert done.wait(5), "capacity refusal must not wait for a callback deadline"
    assert_block(overflow["value"], "callback_busy")
    rig.start_gate.set()
    with rig.condition:
        assert rig.condition.wait_for(lambda: len(rig.calls) == 32, timeout=5)
    assert len(rig.calls) == 32
    rig.deadlines.set()
    for result, finished in pending:
        assert finished.wait(5)
        assert_block(result["value"])
    assert_block(rig.fire("later"), "callback_hung")


def test_completion_at_deadline_leaves_no_phantom_abandoned_worker(rig):
    rig.hold("a")
    rig.finish_before_expiry = True
    assert_block(rig.fire("a"))
    rig.drain()
    rig.verdicts["b"] = "evaluated"
    assert rig.fire("b") == ["evaluated"]


@pytest.mark.parametrize("failure", ["construct", "start"])
def test_start_failure_releases_reserved_capacity(rig, failure):
    rig.start_failure = failure
    assert_block(rig.fire("a"))
    rig.drain()
    rig.start_failure = False
    rig.verdicts["b"] = "evaluated"
    assert rig.fire("b") == ["evaluated"]


def test_duplicate_running_policy_call_is_a_nonempty_block(rig):
    rig.deadlines.clear()
    rig.hold("a")
    pending, done = rig.client(lambda: rig.fire("a"))
    with rig.condition:
        assert rig.condition.wait_for(lambda: len(rig.calls) == 1, timeout=5)
    assert_block(rig.fire("a"))
    assert len(rig.calls) == 1
    rig.deadlines.set()
    assert done.wait(5)
    assert_block(pending["value"])


@pytest.mark.parametrize("capacity", ["hung", "busy"])
@pytest.mark.parametrize("other_action", ["approve", "modify"])
def test_capacity_veto_beats_other_plugin_and_never_writes(rig, monkeypatch, tmp_path, capacity, other_action):
    from model_tools import handle_function_call
    target = tmp_path / "protected.txt"
    target.write_text("unchanged")
    count = 2 if capacity == "hung" else 32
    rig.hold(*(str(i) for i in range(count)))
    pending = []
    if capacity == "busy":
        rig.deadlines.clear()
        pending = [rig.client(lambda i=i: rig.fire(str(i))) for i in range(count)]
        with rig.condition:
            assert rig.condition.wait_for(lambda: len(rig.calls) == count, timeout=5)
    else:
        for i in range(count):
            assert_block(rig.fire(str(i)))

    def other(**_kwargs):
        return {"action": other_action, "message": "another plugin", "args": {"path": str(target)}}

    rig.manager._hooks["pre_tool_call"].append(other)
    writes = []

    def write(*_args, **_kwargs):
        writes.append(True)
        target.write_text("changed")
        return json.dumps({"ok": True})

    registry = MagicMock()
    registry.dispatch.side_effect = write
    monkeypatch.setattr("model_tools.registry", registry)
    result = handle_function_call("write_file", {"path": str(target), "content": "changed"}, session_id="s", tool_call_id="write")
    assert f"callback_{capacity}" in result
    assert writes == []
    assert target.read_text() == "unchanged"
    rig.deadlines.set()
    for held in rig.holds.values():
        held.set()
    for _, done in pending:
        assert done.wait(5)
    rig.drain()
    assert writes == []  # late approval never resurrects the original write


def test_late_approval_never_executes_timed_out_write(rig, monkeypatch, tmp_path):
    from model_tools import handle_function_call
    target = tmp_path / "target"
    target.write_text("original")
    rig.hold("write")
    rig.verdicts["write"] = {"action": "approve", "message": "late"}
    registry = MagicMock()
    monkeypatch.setattr("model_tools.registry", registry)
    result = handle_function_call("write_file", {"path": str(target), "content": "changed"}, tool_call_id="write")
    assert "plugin" in result
    rig.holds["write"].set()
    rig.drain()
    registry.dispatch.assert_not_called()
    assert target.read_text() == "original"


@pytest.mark.parametrize("hook", ["post_tool_call", "transform_tool_result"])
def test_observer_timeout_and_hung_refusal_remain_fail_open(rig, hook):
    rig.manager._hooks[hook] = [rig.callback, lambda **_kw: None]
    rig.hold("a", "b")
    assert rig.fire("a", hook, result="original") == []
    assert rig.fire("b", hook, result="original") == []
    assert rig.fire("c", hook, result="original") == []
    assert [c[0] for c in rig.calls] == ["a", "b"]
    for hold in rig.holds.values():
        hold.set()
    rig.drain()
    rig.verdicts["d"] = "original"
    assert rig.fire("d", hook, result="original") == ["original"]


def test_repeated_just_slow_calls_each_veto_but_bookkeeping_drains(rig):
    for call in ("a", "b", "c"):
        rig.hold(call)
        assert_block(rig.fire(call))
        rig.holds[call].set()
        rig.drain()
    assert [c[0] for c in rig.calls] == ["a", "b", "c"]


def test_callback_hook_and_manager_accounting_are_independent(rig):
    rig.hold("a", "b")
    assert_block(rig.fire("a"))
    assert_block(rig.fire("b"))
    # The same callback under another hook/manager still gets a real evaluation.
    rig.manager._hooks["post_tool_call"] = [rig.callback]
    rig.verdicts["c"] = "observer"
    assert rig.fire("c", "post_tool_call") == ["observer"]
    other = plugins.PluginManager()
    other._hooks["pre_tool_call"] = [rig.callback]
    assert other.invoke_hook("pre_tool_call", tool_call_id="d") == []
    assert not other._hook_running_callbacks
    # A fresh registration has no inherited timer state.
    rig.manager._hooks["pre_tool_call"] = [lambda **_kw: "new registration"]
    assert rig.fire("e") == ["new registration"]


def test_old_generation_completion_cannot_erase_new_running_token(rig):
    callback = rig.manager._hooks["pre_tool_call"][0]
    rig.hold("a")
    old_hold = rig.holds["a"]
    assert_block(rig.fire("a"))
    old_worker = rig.workers[0]
    rig.manager._reset_after_unload_all([])
    rig.manager._hooks["pre_tool_call"] = [callback]
    rig.hold("a")
    assert_block(rig.fire("a"))
    old_hold.set()
    old_worker.join(5)
    assert not old_worker.is_alive()
    assert len(rig.manager._hook_running_callbacks) == 1
    assert_block(rig.fire("a"))
    assert len(rig.calls) == 2


@pytest.mark.parametrize("hook", ["pre_tool_call", "post_tool_call"])
@pytest.mark.parametrize("failure", ["timeout", "exception"])
def test_async_policy_timeout_and_exception_stay_fail_closed(monkeypatch, hook, failure):
    import asyncio
    manager = plugins.PluginManager()
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0.001)
    later = []

    async def callback(**_kwargs):
        if failure == "exception":
            raise RuntimeError("policy failed")
        await asyncio.Event().wait()

    manager._hooks[hook] = [callback, lambda **_kw: later.append(True)]
    results = asyncio.run(manager.ainvoke_hook(hook))
    if hook == "pre_tool_call":
        assert_block(results)
    else:
        assert results == []
    assert later == [True]
    assert not manager._hook_running_callbacks  # async path has no threaded budget
