"""Cross-process busy routing contracts for zero-cut handover."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.outbox import Outbox
from gateway.outbox import _recover_locked
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, SessionStore
from gateway.zero_cut_busy import (
    ZeroCutBusyRoutingMixin,
    _marker,
    queue_foreign_owner,
    watch_foreign_queues,
)


def _source(chat_id="busy-chat"):
    return SessionSource(platform=Platform.DISCORD, chat_id=chat_id, user_id="u", chat_type="dm")


def _stores(tmp_path, enabled=True):
    from hermes_state import SessionDB

    db_path = tmp_path / "state.db"
    config = GatewayConfig(zero_cut_handover_enabled=enabled)
    stores = [SessionStore(tmp_path / "sessions", config) for _ in range(2)]
    for store in stores:
        store._db = SessionDB(db_path)
    return stores


class _Adapter:
    """Only the adapter starts session processing (the runner has no such method), so the fake keeps
    that receiver: a fake runner method once hid a production AttributeError that lost the follow-up."""

    def __init__(self):
        self._pending_messages = {}
        self._active_sessions = {}
        self.drained = []

    def _start_session_processing(self, event, key):
        self.drained.append((key, event.message_id))
        return True


class _Runner:
    def __init__(self, store, config, adapter):
        self.async_session_store = store
        self.config = config
        self.adapters = {"discord": adapter}
        self._zero_cut_busy_keys = set()
        self.agent_admissions = 0

    def _queue_or_replace_pending_event(self, key, event):
        adapter = self.adapters["discord"]
        if key not in adapter._pending_messages:
            adapter._pending_messages[key] = event
            return True
        overflow = getattr(adapter, "_overflow", {})
        overflow.setdefault(key, []).append(event)
        adapter._overflow = overflow
        return True

    @property
    def drained(self):
        return self.adapters["discord"].drained


def test_two_store_direct_marker_read_sees_owner_and_clear(tmp_path):
    a, b = _stores(tmp_path)
    entry = a.get_or_create_session(_source())
    token = a.mark_turn_active(entry.session_key)
    marker = _marker(b, entry.session_key)
    assert marker == {"token": token, "pid": os.getpid(), "slot": "a"}
    assert a.clear_turn_active(entry.session_key, token)
    assert _marker(b, entry.session_key) is None


@pytest.mark.asyncio
async def test_live_foreign_owner_queues_without_agent_admission(tmp_path, monkeypatch):
    a, b = _stores(tmp_path)
    entry = a.get_or_create_session(_source())
    a.mark_turn_active(entry.session_key)
    adapter = _Adapter()
    runner = _Runner(b, b.config, adapter)
    event = MessageEvent("follow-up", source=_source(), message_id="m-2")
    monkeypatch.setattr("gateway.zero_cut_busy.os.getpid", lambda: 9002)
    monkeypatch.setattr("gateway.zero_cut_busy.os.kill", lambda pid, sig: None)
    assert await queue_foreign_owner(runner, event, entry.session_key)
    assert runner.agent_admissions == 0
    assert adapter._pending_messages[entry.session_key] is event
    assert runner._zero_cut_busy_keys == {entry.session_key}


@pytest.mark.asyncio
async def test_marker_clear_wakes_one_turn_without_new_inbound(tmp_path):
    a, b = _stores(tmp_path)
    entry = a.get_or_create_session(_source())
    token = a.mark_turn_active(entry.session_key)
    adapter = _Adapter()
    runner = _Runner(b, b.config, adapter)
    event = MessageEvent("follow-up", source=_source(), message_id="m-2")
    adapter._pending_messages[entry.session_key] = event
    runner._zero_cut_busy_keys.add(entry.session_key)

    task = asyncio.create_task(watch_foreign_queues(runner))
    await asyncio.sleep(0.05)
    assert a.clear_turn_active(entry.session_key, token)
    await asyncio.wait_for(_wait_for(lambda: len(runner.drained) == 1), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert runner.drained == [(entry.session_key, "m-2")]


@pytest.mark.asyncio
async def test_dead_owner_clears_without_resume_and_drains_once(tmp_path, monkeypatch):
    a, b = _stores(tmp_path)
    entry = a.get_or_create_session(_source())
    token = a.mark_turn_active(entry.session_key)
    raw = a._db.load_gateway_routing_entry(entry.session_key, scope=a._routing_scope())
    payload = json.loads(raw)
    payload["active_turn_owner_pid"] = 777777
    a._db.save_gateway_routing_entry(entry.session_key, json.dumps(payload), scope=a._routing_scope())
    adapter = _Adapter()
    runner = _Runner(b, b.config, adapter)
    event = MessageEvent("follow-up", source=_source(), message_id="m-2")
    adapter._pending_messages[entry.session_key] = event
    runner._zero_cut_busy_keys.add(entry.session_key)
    monkeypatch.setattr("gateway.zero_cut_busy.os.getpid", lambda: 9002)

    task = asyncio.create_task(watch_foreign_queues(runner))
    await asyncio.wait_for(_wait_for(lambda: len(runner.drained) == 1), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _marker(b, entry.session_key) is None
    assert not b._entries.get(entry.session_key, SimpleNamespace(resume_pending=False)).resume_pending
    assert runner.drained == [(entry.session_key, "m-2")]
    assert token


@pytest.mark.asyncio
async def test_waiting_key_keeps_queueing_after_marker_clears_so_order_holds(tmp_path, monkeypatch):
    a, b = _stores(tmp_path)
    entry = a.get_or_create_session(_source())
    token = a.mark_turn_active(entry.session_key)
    adapter = _Adapter()
    runner = _Runner(b, b.config, adapter)
    monkeypatch.setattr("gateway.zero_cut_busy.os.getpid", lambda: 9002)
    monkeypatch.setattr("gateway.zero_cut_busy.os.kill", lambda pid, sig: None)
    first = MessageEvent("first", source=_source(), message_id="m-2")
    assert await queue_foreign_owner(runner, first, entry.session_key)
    assert a.clear_turn_active(entry.session_key, token)  # A finished; watcher has not drained yet
    second = MessageEvent("second", source=_source(), message_id="m-3")
    assert await queue_foreign_owner(runner, second, entry.session_key)  # must not start ahead of m-2
    assert adapter._pending_messages[entry.session_key] is first
    assert adapter._overflow[entry.session_key] == [second]


@pytest.mark.asyncio
async def test_unqueued_session_with_cleared_marker_is_not_routed(tmp_path, monkeypatch):
    a, b = _stores(tmp_path)
    entry = a.get_or_create_session(_source())
    token = a.mark_turn_active(entry.session_key)
    assert a.clear_turn_active(entry.session_key, token)
    runner = _Runner(b, b.config, _Adapter())
    monkeypatch.setattr("gateway.zero_cut_busy.os.getpid", lambda: 9002)
    event = MessageEvent("new", source=_source(), message_id="m-9")
    assert await queue_foreign_owner(runner, event, entry.session_key) is False


async def _wait_for(predicate):
    while not predicate():
        await asyncio.sleep(0.02)


def test_fifo_burst_and_cap_are_left_to_existing_queue(tmp_path):
    _, b = _stores(tmp_path)
    entry = b.get_or_create_session(_source())
    adapter = _Adapter()
    runner = _Runner(b, b.config, adapter)
    events = [MessageEvent(str(i), source=_source(), message_id=f"m-{i}") for i in range(3)]
    for event in events:
        assert runner._queue_or_replace_pending_event(entry.session_key, event)
    assert list(adapter._pending_messages.values())[0].message_id == "m-0"
    assert len(adapter._pending_messages) == 1
    assert [event.message_id for event in events] == ["m-0", "m-1", "m-2"]


@pytest.mark.asyncio
async def test_outbox_ambiguous_is_not_replayed_and_pending_claims_once(tmp_path):
    one, two = Outbox(Path(tmp_path) / "home"), Outbox(Path(tmp_path) / "home")
    ambiguous = one.enqueue("turn-a", "send", {"text": "already maybe sent"})
    assert one.begin_send(ambiguous)

    class Adapter:
        sends = 0

        async def send(self, **_payload):
            self.sends += 1

    adapter = Adapter()
    assert await _recover_locked(two, adapter, startup=True) == (0, 1)
    assert adapter.sends == 0
    pending = one.enqueue("turn-b", "send", {"text": "retry"})
    assert two.begin_send(pending)
    assert not one.begin_send(pending)

def test_flag_off_does_not_read_marker_or_start_watcher(tmp_path, monkeypatch):
    a, b = _stores(tmp_path, enabled=False)
    entry = a.get_or_create_session(_source())
    token = a.mark_turn_active(entry.session_key)
    raw = a._db.load_gateway_routing_entry(entry.session_key, scope=a._routing_scope())
    assert "active_turn_owner_pid" not in json.loads(raw)
    assert token
    adapter = _Adapter()
    runner = _Runner(b, b.config, adapter)
    monkeypatch.setattr("gateway.zero_cut_busy._marker", lambda *_: (_ for _ in ()).throw(AssertionError("read")))
    assert not asyncio.run(queue_foreign_owner(runner, MessageEvent("x", source=_source()), "key"))

    class StartupProbe(ZeroCutBusyRoutingMixin):
        config = GatewayConfig(zero_cut_handover_enabled=False)
        def _spawn_supervised(self, *_args, **_kwargs):
            raise AssertionError("watcher started")

    StartupProbe()._start_zero_cut_busy_watcher()


def test_runner_has_no_drain_method_the_watcher_could_call_by_mistake():
    from gateway.run import GatewayRunner
    assert not hasattr(GatewayRunner, "_spawn_drain_task")


@pytest.mark.asyncio
async def test_watcher_leaves_a_follow_up_to_a_live_local_turn(tmp_path):
    a, b = _stores(tmp_path)
    entry = a.get_or_create_session(_source())
    adapter = _Adapter()
    runner = _Runner(b, b.config, adapter)
    event = MessageEvent("follow-up", source=_source(), message_id="m-2")
    adapter._pending_messages[entry.session_key] = event
    adapter._active_sessions[entry.session_key] = asyncio.Event()  # B's own turn owns the chat
    runner._zero_cut_busy_keys.add(entry.session_key)
    task = asyncio.create_task(watch_foreign_queues(runner))
    await asyncio.wait_for(_wait_for(lambda: not runner._zero_cut_busy_keys), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.drained == []
    assert adapter._pending_messages[entry.session_key] is event  # the local turn drains it
