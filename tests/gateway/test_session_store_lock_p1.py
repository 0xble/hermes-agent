"""P1 regression tests for SessionStore lock ownership and loop liveness."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway.session_store_async import run_store_call, run_store_lookup


class _HeldStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.release = threading.Event()
        self.order = []

    def lookup_by_session_key(self, key):
        self.started.set()
        with self.lock:
            self.release.wait()
            self.order.append(key)
            return SimpleNamespace(session_key=key, session_id=f"id:{key}")


@pytest.mark.asyncio
async def test_store_executor_preserves_hermes_home_contextvar(tmp_path):
    from hermes_constants import (
        get_hermes_home_override,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    token = set_hermes_home_override(tmp_path)
    try:
        seen = await run_store_call(get_hermes_home_override)
    finally:
        reset_hermes_home_override(token)
    assert seen == str(tmp_path)


@pytest.mark.asyncio
async def test_store_lookup_preserves_hermes_home_contextvar(tmp_path):
    from hermes_constants import (
        get_hermes_home_override,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    store = SimpleNamespace(read=get_hermes_home_override)
    token = set_hermes_home_override(tmp_path)
    try:
        seen = await run_store_lookup(store, "read")
    finally:
        reset_hermes_home_override(token)
    assert seen == str(tmp_path)


async def _assert_async_entry_yields(task_factory, lock, entered, release):
    lock.acquire()
    task = asyncio.create_task(task_factory())
    assert await asyncio.to_thread(entered.wait)
    tick = asyncio.Event()
    asyncio.get_running_loop().call_soon(tick.set)
    await tick.wait()
    assert not task.done()
    release.set()
    lock.release()
    return await task


@pytest.mark.asyncio
async def test_check_native_route_yields_while_store_lock_is_held(monkeypatch):
    import gateway.session_envelope as envelope

    lock = threading.Lock()
    entered, release = threading.Event(), threading.Event()
    source = SimpleNamespace(platform=SimpleNamespace(value="local"))
    adapter = object()
    event = SimpleNamespace(source=source)

    class Store:
        def _generate_session_key(self, _source):
            return "route"

        def lookup_by_session_key(self, _route):
            entered.set()
            with lock:
                release.wait()
            return SimpleNamespace(session_id="session")

    runner = SimpleNamespace(
        session_store=Store(),
        _adapter_for_source=lambda _source: adapter,
    )
    payload = {"native_text_v1": {"source": {}, "route": "route"}}
    monkeypatch.setattr("gateway.session_envelope.restore_native", lambda *_args: event)
    monkeypatch.setattr(envelope, "_validate_native", lambda *args: None)
    async def reauthorize_roles(*_args):
        return False

    monkeypatch.setattr("gateway.session_ingress_context.reauthorize_roles", reauthorize_roles)
    result = await _assert_async_entry_yields(
        lambda: envelope.check_native_route(runner, payload, "session", source, adapter),
        lock, entered, release,
    )
    assert result == (source, "route")


@pytest.mark.asyncio
async def test_restore_local_session_yields_while_store_lock_is_held(monkeypatch):
    import gateway.session_local_recovery as recovery
    from gateway.config import Platform

    lock = threading.Lock()
    entered, release = threading.Event(), threading.Event()
    sid, live, prepared, adapter = "sid", object(), object(), object()
    authority = SimpleNamespace(
        sessions={sid: live},
        _require_admission_open=lambda: None,
    )

    def restore_entry(*args, **kwargs):
        entered.set()
        with lock:
            release.wait()

    monkeypatch.setattr(recovery, "_read_local_session", lambda *_args: prepared)
    monkeypatch.setattr(recovery, "_local_restore_adapter", lambda _authority: adapter)
    monkeypatch.setattr(recovery, "_check_restored_live", lambda *args: None)
    monkeypatch.setattr(recovery, "_restore_local_entry", restore_entry)
    monkeypatch.setattr(recovery, "local_adapter_map", lambda _authority: {Platform.LOCAL: adapter})
    monkeypatch.setattr(recovery, "_publish_local_session", lambda *args: "published")
    result = await _assert_async_entry_yields(
        lambda: recovery.restore_local_session_async(authority, sid),
        lock, entered, release,
    )
    assert result == "published"


@pytest.mark.asyncio
async def test_session_title_lookup_yields_while_store_lock_is_held():
    from gateway.turn_session_titles import TurnSessionTitlesMixin

    lock = threading.Lock()
    entered, release = threading.Event(), threading.Event()

    class Store:
        def peek_session_id(self, _session_key):
            entered.set()
            with lock:
                release.wait()
            return "sid"

    class Adapter:
        def configure_session_titles(self, _agent, _ctx, callback):
            self.callback = callback
            return True

    adapter = Adapter()
    runner = SimpleNamespace(session_store=Store())
    agent = SimpleNamespace(session_id="sid")
    ctx = SimpleNamespace(session_key="key", _status_adapter=adapter)
    mixin = TurnSessionTitlesMixin()
    mixin._runner = runner
    mixin._attach_session_title_callback(agent, ctx)
    result = await _assert_async_entry_yields(
        lambda: adapter.callback(), lock, entered, release,
    )
    assert result is True


@pytest.mark.asyncio
async def test_model_override_rehydrate_yields_while_store_lock_is_held(monkeypatch):
    from contextlib import asynccontextmanager
    from gateway.run_agent_cache import GatewayAgentCacheMixin

    lock = threading.Lock()
    entered, release = threading.Event(), threading.Event()

    class Store:
        def get_model_role(self, _session_key):
            entered.set()
            with lock:
                release.wait()
            return "role"

    runner = GatewayAgentCacheMixin()
    runner.session_store = Store()
    state = SimpleNamespace(conversation=SimpleNamespace(model_role=None))

    @asynccontextmanager
    async def override_lock(_session_key):
        yield

    runner._session_model_override_lock = override_lock
    runner._session_state = lambda _session_key: state
    runner._session_model_override = lambda _session_key: None
    runner._session_model_override_epoch = lambda _session_key: 0
    runner._load_session_model_override = lambda _session_key: (None, "session")
    runner._commit_session_model_override = lambda *args: None
    result = await _assert_async_entry_yields(
        lambda: runner._rehydrate_session_model_selection_async("session"),
        lock, entered, release,
    )
    assert result is None
    assert state.conversation.model_role == "role"


@pytest.mark.asyncio
async def test_snapshot_native_async_runs_legacy_retry_scan_off_loop(monkeypatch):
    import gateway.session_envelope as envelope

    loop_thread = threading.get_ident()
    seen_threads = []
    payload = {"native_text_v1": {"route": "route"}}
    monkeypatch.setattr(envelope, "_native_payload", lambda *args, **kwargs: payload)

    def legacy_retry(*args):
        seen_threads.append(threading.get_ident())
        return args[2]

    monkeypatch.setattr(envelope, "_legacy_retry_payload", legacy_retry)
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=False),
        session_store=SimpleNamespace(lookup_by_session_key=lambda _route: object()),
    )
    result = await envelope._snapshot_native_async(runner, object(), object())
    assert result is payload
    assert len(seen_threads) == 1
    assert seen_threads[0] != loop_thread


@pytest.mark.asyncio
async def test_automation_route_owner_lookup_runs_off_loop(monkeypatch):
    import gateway.session_automation as automation

    loop_thread = threading.get_ident()
    seen_threads = []
    lock = threading.Lock()
    entered, release = threading.Event(), threading.Event()
    authority = object()
    event = object()
    entry = object()

    def owner(*args):
        seen_threads.append(threading.get_ident())
        entered.set()
        with lock:
            release.wait()
        return entry

    monkeypatch.setattr(automation, "_owner", owner)
    monkeypatch.setattr(automation, "_check_automation_route", lambda *args: "ok")
    monkeypatch.setattr("gateway.session_envelope.restore_native", lambda *_args: event)
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda _runner: authority)
    result = await _assert_async_entry_yields(
        lambda: automation.check_automation_route_async(
            SimpleNamespace(session_store=object()), {}, "session", object(), object()),
        lock, entered, release,
    )
    assert result == "ok"
    assert len(seen_threads) == 1
    assert seen_threads[0] != loop_thread


@pytest.mark.asyncio
async def test_store_lookup_does_not_block_loop_when_lock_is_held():
    store = _HeldStore()
    store.lock.acquire()
    task = asyncio.create_task(run_store_lookup(store, "lookup_by_session_key", "head"))
    assert await asyncio.to_thread(store.started.wait)
    tick = asyncio.Event()
    asyncio.get_running_loop().call_soon(tick.set)
    await tick.wait()
    assert not task.done()
    store.release.set()
    store.lock.release()
    result = await task
    assert result.session_key == "head"


@pytest.mark.asyncio
async def test_cancelled_store_lookup_never_publishes_late_result():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    published = []

    class Store:
        def read(self):
            entered.set()
            release.wait()
            finished.set()
            return "stale"

    task = asyncio.create_task(run_store_lookup(Store(), "read"))
    await asyncio.to_thread(entered.wait)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.to_thread(finished.wait)
    assert finished.is_set()
    assert published == []


def test_store_lookup_executor_is_bounded_and_dedicated():
    from gateway.session_store_async import session_store_executor

    assert session_store_executor()._max_workers <= 4
    assert session_store_executor()._thread_name_prefix.startswith("session-store")


def test_liveness_dump_includes_session_store_owner_snapshot():
    from hermes_loop_guard import LoopGuardedLock
    from gateway.shutdown_watchdog import session_store_lock_snapshot

    lock = LoopGuardedLock("session-store lock")
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock():
        with lock:
            acquired.set()
            release.wait()

    holder = threading.Thread(target=hold_lock, name="store-writer")
    holder.start()
    assert acquired.wait()
    try:
        snapshot = session_store_lock_snapshot(SimpleNamespace(
            session_store=SimpleNamespace(_lock=lock)))
    finally:
        release.set()
        holder.join()
    detail = snapshot["session_store_locks"]["default"]
    assert detail["acquisition_count"] == 1
    assert detail["owner"]["thread_name"] == "store-writer"
