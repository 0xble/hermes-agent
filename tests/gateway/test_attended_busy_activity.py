"""Busy replies refresh attended routing at receipt, never when their queue drains."""
import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import run as gateway_run
from gateway import zero_cut_busy
from gateway.attended_model import AttendedResolution
from gateway.config import GatewayConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import Platform, SessionSource, SessionStore


CONFIG = {"model": {
    "default": "default", "provider": "nous", "idle_revert_minutes": 15,
    "attended": {"model": "opus", "provider": "anthropic", "reasoning_effort": "high"},
}}


@pytest.fixture
def busy_activity(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._draining = False
    runner._busy_text_mode = "interrupt"
    runner._background_tasks = set()
    adapter = SimpleNamespace(_pending_messages={})
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda _source: adapter
    event = MessageEvent(text="please also check this", source=SessionSource(
        platform=Platform.TELEGRAM, chat_id="123", user_id="owner",
    ))
    key = runner._session_key_for_source(event.source)
    state = runner._session_state(key)
    state.conversation.last_user_activity_at = 1.0
    state.turn.agent = Mock()
    state.turn.started_ts = 0
    state.turn.busy_ack_ts = 0
    runner.session_store = SimpleNamespace()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        lookup_by_session_key=AsyncMock(return_value=SimpleNamespace(
            session_key=key, created_at="created", updated_at="updated",
        )),
        mark_user_activity=AsyncMock(),
    )
    runner._is_user_authorized_for_source = lambda _source: True
    runner._admit_bot_message_for_source = lambda _source: True
    runner._route_plaintext_approval_while_busy = AsyncMock(return_value=False)
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    runner._hm_busy_slash_or_photo = AsyncMock(return_value=(False, None))
    runner._hm_is_registered_command = lambda _event: False
    runner._relay_fyi_passive_enabled = lambda _source=None: False
    runner._agent_has_active_subagents = lambda _agent: False
    runner._steer_running_agent = Mock(return_value=True)
    runner._fold_into_running_turn = Mock()
    runner._hm_busy_interrupt = AsyncMock()
    runner._queue_or_replace_pending_event = Mock(return_value=True)
    runner._enqueue_fifo = Mock(return_value=True)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: CONFIG)
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    return runner, event, key, state


async def _receive(fixture, path, mode, monkeypatch):
    runner, event, key, state = fixture
    runner._busy_input_mode = mode
    if path == "adapter_text_queue":
        runner._busy_text_mode = "queue"
    if path == "grace":
        state.turn.started_ts = time.time()
    if path.startswith("foreign"):
        monkeypatch.setattr(zero_cut_busy, "_enabled", lambda _runner: True)
        monkeypatch.setattr(zero_cut_busy, "_marker", lambda *_args: {
            "token": "foreign", "pid": -1, "slot": "old",
        })
        monkeypatch.setattr(zero_cut_busy, "_owner_live", lambda _pid: True)
        runner._queue_or_replace_pending_event.return_value = path == "foreign_accept"
        await zero_cut_busy.queue_foreign_owner(runner, event, key)
    elif path.startswith("adapter"):
        result = await runner._handle_active_session_busy_message(event, key)
        if path == "adapter_text_queue":
            if event.internal and event.allow_gateway_control:
                assert result is True
                runner._queue_or_replace_pending_event.assert_called_once_with(key, event)
                assert getattr(event, "_gateway_accepted", False) is True
                runner._steer_running_agent.assert_not_called()
                runner._hm_busy_interrupt.assert_not_awaited()
            else:
                assert result is False
                runner._queue_or_replace_pending_event.assert_not_called()
    elif path == "command":
        runner._hm_admit_event = AsyncMock(return_value=(event, event.source, event.internal))
        runner._quick_command_alias_text = lambda _event: None
        runner._hm_estop_gate = lambda *_args: None
        runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
        runner._hm_dispatch_idle_commands = AsyncMock(return_value=(True, "status"))
        monkeypatch.setattr(zero_cut_busy, "queue_foreign_owner", AsyncMock(return_value=False))
        monkeypatch.setattr(zero_cut_busy, "handle_local_busy", AsyncMock(return_value=zero_cut_busy._NOT_HANDLED))
        assert await runner._handle_admitted_message(event) == "status"
    else:
        await runner._hm_handle_running_session_message(event, event.source, key)


BUSY_PATHS = [(path, mode) for path in ("adapter", "runner") for mode in ("queue", "steer", "interrupt")]
BUSY_PATHS += [("grace", "interrupt"), ("adapter_text_queue", "queue"),
               ("foreign_accept", "queue"), ("foreign_reject", "queue"), ("command", "queue")]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,mode", BUSY_PATHS)
@pytest.mark.parametrize("kind", ["human", "relay", "goal", "process", "bot", "attend_invalid", "attend_status", "attend_off", "attend_attachment"])
async def test_only_human_receipts_refresh_once_without_blocking(busy_activity, monkeypatch, path, mode, kind):
    runner, event, key, state = busy_activity
    if path == "command":
        event.text = "/status"
    if kind.startswith("attend_"):
        action = {"attend_invalid": "0m", "attend_status": "status", "attend_off": "off", "attend_attachment": "0m"}[kind]
        event.text = "/attend@HermesBot " + action
        if kind == "attend_attachment":
            event.text = "@image:/path/photo.png\n\n" + event.text
    elif kind == "relay":
        event.text = "[relay from=hermes:default/other]\nFYI"
        event.metadata = {"notification_origin": "relay_delivery"}
    elif kind == "goal":
        event.internal = True
    elif kind == "process":
        event.metadata = {"kind": "process_notification"}
    elif kind == "bot":
        event.source.is_bot = True
    release = asyncio.Event()

    async def lookup(_key):
        await release.wait()
        return SimpleNamespace(session_key=key, created_at="created", updated_at="updated")

    runner.async_session_store.lookup_by_session_key.side_effect = lookup
    before = time.time()
    await _receive(busy_activity, path, mode, monkeypatch)
    received = time.time()
    assert state.conversation.last_user_activity_at == 1.0
    task = getattr(event, "_attended_activity_task", None)
    if kind != "human":
        assert task is None
        runner.async_session_store.mark_user_activity.assert_not_awaited()
        return
    assert task is not None and task in runner._background_tasks
    release.set()
    # Draining while persistence is in flight joins it rather than stamping twice.
    assert await runner._mark_attended_user_activity(event, event.source, key)
    await task
    stamp = state.conversation.last_user_activity_at
    assert before <= stamp <= received
    assert event._attended_activity_marked is True
    runner.async_session_store.mark_user_activity.assert_awaited_once_with(
        key, when=datetime.fromtimestamp(stamp),
    )
    assert await runner._mark_attended_user_activity(event, event.source, key)
    assert state.conversation.last_user_activity_at == stamp
    runner.async_session_store.mark_user_activity.assert_awaited_once()
    if path == "grace":
        runner._hm_busy_interrupt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
async def test_api_boundary_refreshes_same_session_durable_activity_off_loop(
    busy_activity, monkeypatch, caplog, tmp_path, foreign,
):
    runner, event, key, state = busy_activity
    owner_store = SessionStore(tmp_path / "sessions", runner.config)
    await asyncio.to_thread(owner_store.get_or_create_session, event.source)
    await asyncio.to_thread(owner_store.mark_user_activity, key)
    old = await asyncio.to_thread(owner_store.get_last_user_activity_at, key)
    assert old is not None and old.tzinfo is None
    writer = SessionStore(tmp_path / "sessions", runner.config) if foreign else owner_store
    runner.session_store = writer
    # Store hydration and receipt persistence both go through the real AsyncSessionStore.
    before = time.time()
    await _receive(busy_activity, "runner", "steer", monkeypatch)
    await event._attended_activity_task
    first = await asyncio.to_thread(writer.get_last_user_activity_at, key)
    assert first is not None and first.timestamp() >= before and first > old
    assert first.tzinfo is None
    assert state.conversation.last_user_activity_at == pytest.approx(first.timestamp(), rel=0, abs=1e-6)
    assert await asyncio.to_thread(writer.list_sessions, active_minutes=15)
    assert await asyncio.to_thread(writer.prune_old_entries, max_age_days=1) == 0
    next_event = MessageEvent(text="another thing", source=event.source)
    await runner._mark_attended_user_activity(next_event, event.source, key)
    latest = await asyncio.to_thread(writer.get_last_user_activity_at, key)
    assert latest is not None and latest > first
    # An out-of-order worker must never roll the persisted stamp back.
    assert not await asyncio.to_thread(writer.mark_user_activity, key, old)
    reader = SessionStore(tmp_path / "sessions", runner.config)
    assert await asyncio.to_thread(reader.get_last_user_activity_at, key) == latest
    runner.session_store = owner_store
    if foreign:
        state.conversation.last_user_activity_at = 1.0
    previous = AttendedResolution("opus", "anthropic", "high", "attended", True, idle_model="default")
    state.conversation.last_attended_decision = previous
    runner._load_attended_route_active = lambda _key: True
    runner._session_model_override = lambda _key: None
    runner._session_reasoning_override = lambda _key: None
    runner._load_reasoning_config = lambda *_args, **_kwargs: {}
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = SimpleNamespace(session_key=key, source=event.source, user_config=CONFIG)
    agent = SimpleNamespace(
        model="opus", provider="anthropic", api_mode="anthropic_messages",
        _gateway_attended_decision_key=previous.decision_key,
    )
    switch = Mock()
    monkeypatch.setattr("agent.agent_runtime_helpers.switch_model", switch)
    loop_thread = threading.get_ident()
    reads = []
    loader = owner_store._routing_db_method("load_gateway_routing_entry")
    assert loader is not None

    def checked_load(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        reads.append(1)
        return loader(*args, **kwargs)

    monkeypatch.setattr(owner_store._routing_db, "load_gateway_routing_entry", checked_load)
    # TurnRunner.run_sync uses the gateway executor in production.
    await asyncio.to_thread(turn._apply_attended_before_api_call, agent)
    assert reads
    assert agent._gateway_attended_decision.reason == "attended"
    assert "attended model refresh before API call failed" not in caplog.text
    assert agent.model == "opus"
    switch.assert_not_called()
    assert await asyncio.to_thread(owner_store.get_last_user_activity_at, key) == latest
    # A newer stamp belonging to a reset session must not leak into the old turn.
    await asyncio.to_thread(reader.reset_session, key)
    await asyncio.to_thread(reader.mark_user_activity, key)
    assert await asyncio.to_thread(owner_store.get_last_user_activity_at, key, refresh=True) == latest
    for store in (owner_store, writer, reader):
        await asyncio.to_thread(store.close_all_db_handles)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_stamp", [None, 2.0])
async def test_worker_handoff_keeps_a_receipt_that_arrives_before_loop_apply(
    busy_activity, worker_stamp,
):
    runner, event, key, state = busy_activity
    state.conversation.last_user_activity_at = worker_stamp
    queued = []
    runner._gateway_loop = SimpleNamespace(
        is_running=lambda: True, call_soon_threadsafe=queued.append,
    )
    runner._load_reasoning_config = lambda *_args, **_kwargs: {}
    await asyncio.to_thread(
        runner._attended_resolution_for_session,
        source=event.source, session_key=key, user_config=CONFIG,
        activity_stamp=worker_stamp,
    )
    assert queued
    # The receipt runs on the owning loop after the worker read but before its apply.
    assert await runner._mark_attended_user_activity(event, event.source, key)
    receipt = state.conversation.last_user_activity_at
    assert receipt is not None and receipt > (worker_stamp or 0)
    for apply in queued:
        apply()
    assert state.conversation.last_user_activity_at == receipt


@pytest.mark.parametrize("offset", [-7, 5.5])
def test_aware_activity_rows_and_writes_keep_store_clocks_comparable(tmp_path, offset, monkeypatch):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="aware", user_id="owner")
    writer = SessionStore(tmp_path / "sessions", GatewayConfig())
    entry = writer.get_or_create_session(source)
    key = entry.session_key
    old = datetime.fromtimestamp(1)
    writer.mark_user_activity(key, old)
    owner = SessionStore(tmp_path / "sessions", GatewayConfig())
    assert owner.get_last_user_activity_at(key) == old
    zone = timezone(timedelta(hours=offset))
    aware = datetime.fromtimestamp(time.time() - 60, zone)
    payload = entry.to_dict()
    payload.update(last_user_activity_at=aware.isoformat(), updated_at=aware.isoformat())
    # Reproduce a routing row written by the earlier candidate, not a mock read.
    writer._save_entry(key, entry_data=payload)
    reader = SessionStore(tmp_path / "sessions", GatewayConfig())
    try:
        expected = datetime.fromtimestamp(aware.timestamp())
        assert reader.get_last_user_activity_at(key) == expected
        assert owner.get_last_user_activity_at(key, refresh=True) == expected
        with monkeypatch.context() as cached:
            resolver = Mock(side_effect=AssertionError("cached activity must not resolve the DB"))
            cached.setattr(owner, "_routing_db_method", resolver)
            assert owner.get_last_user_activity_at(key) == expected
            resolver.assert_not_called()
        reader.get_or_create_session(SessionSource(platform=Platform.TELEGRAM, chat_id="naive"))
        assert len(reader.list_sessions()) == 2
        assert len(reader.list_sessions(active_minutes=15)) == 2
        assert reader.prune_old_entries(max_age_days=1) == 0
        assert reader.mark_user_activity(key, datetime.now(zone))
        latest = reader.get_last_user_activity_at(key)
        assert latest is not None and latest.tzinfo is None and latest > expected
        restored = reader.lookup_by_session_key(key)
        assert restored is not None and restored.updated_at == latest
        assert not reader.mark_user_activity(key, aware)
    finally:
        for store in (writer, owner, reader):
            store.close_all_db_handles()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("elapsed", [3000, 3600])
def test_activity_advances_and_survives_reload_during_dst_fallback(tmp_path, monkeypatch, elapsed):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("America/Los_Angeles")
    first = datetime(2026, 11, 1, 1, 30, tzinfo=zone).timestamp()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="dst", user_id="owner")
    stores = []
    try:
        with monkeypatch.context() as local:
            local.setenv("TZ", "America/Los_Angeles")
            time.tzset()
            writer = SessionStore(tmp_path / "sessions", GatewayConfig())
            stores.append(writer)
            key = writer.get_or_create_session(source).session_key
            assert writer.mark_user_activity(key, datetime.fromtimestamp(first))
            owner = SessionStore(tmp_path / "sessions", GatewayConfig())
            stores.append(owner)
            prior = owner.get_last_user_activity_at(key)
            assert prior is not None and prior.timestamp() == first
            # Repeated wall times may be earlier OR equal while the instant advances.
            instant = first + elapsed
            stamp = datetime.fromtimestamp(instant)
            assert stamp.fold == 1
            assert writer.mark_user_activity(key, stamp)
            latest = writer.get_last_user_activity_at(key)
            assert latest is not None and latest.timestamp() == instant
            updated = writer.lookup_by_session_key(key)
            assert updated is not None and updated.updated_at.timestamp() == instant
            refreshed = owner.get_last_user_activity_at(key, refresh=True)
            assert refreshed is not None and refreshed.timestamp() == instant
            reader = SessionStore(tmp_path / "sessions", GatewayConfig())
            stores.append(reader)
            restored = reader.get_last_user_activity_at(key)
            assert restored is not None and restored.timestamp() == instant
            entry = reader.lookup_by_session_key(key)
            assert entry is not None and entry.updated_at.timestamp() == instant
            assert not reader.mark_user_activity(key, datetime.fromtimestamp(first))
    finally:
        time.tzset()
        for store in stores:
            store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["runner", "adapter"])
@pytest.mark.parametrize("drain", [False, True])
async def test_steered_receipt_recovers_without_drain(busy_activity, monkeypatch, path, drain):
    runner, event, key, state = busy_activity
    calls = 0

    async def persist(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient receipt failure")
        return True

    runner.async_session_store.mark_user_activity.side_effect = persist
    await _receive(busy_activity, path, "steer", monkeypatch)
    original = event._attended_activity_task
    stamp = event._attended_activity_received_at
    if drain:
        # A concurrent turn-start must join recovery instead of duplicating its write.
        assert await runner._mark_attended_user_activity(event, event.source, key)
    await asyncio.gather(original, return_exceptions=True)
    await asyncio.gather(event._attended_activity_task, return_exceptions=True)
    # Successful steering consumes the event, so recovery cannot depend on drain.
    runner._steer_running_agent.assert_called_once()
    runner._queue_or_replace_pending_event.assert_not_called()
    assert getattr(event, "_attended_activity_marked", False)
    assert state.conversation.last_user_activity_at == stamp
    assert runner.async_session_store.mark_user_activity.await_count == 2
    for call in runner.async_session_store.mark_user_activity.await_args_list:
        assert call.args == (key,)
        assert call.kwargs == {"when": datetime.fromtimestamp(stamp)}
    runner._load_reasoning_config = lambda *_args, **_kwargs: {}
    decision = runner._attended_resolution_for_session(
        source=event.source, session_key=key, user_config=CONFIG,
        activity_stamp=1.0, allow_switch_back=True,
    )
    assert decision.reason == "attended"


@pytest.mark.asyncio
async def test_steered_receipt_retries_failed_store_write(busy_activity, tmp_path, monkeypatch):
    from gateway.session import AsyncSessionStore, SessionStore

    runner, event, key, _state = busy_activity
    store = SessionStore(tmp_path / "sessions", runner.config)
    reader = SessionStore(tmp_path / "sessions", runner.config)
    try:
        entry = store.get_or_create_session(event.source)
        store.mark_user_activity(key, when=datetime.fromtimestamp(1))
        runner.session_store = store
        runner._async_session_store = AsyncSessionStore(store)
        save_entry = store._save_entry
        attempts = 0

        def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient persistence failure")
            return save_entry(*args, **kwargs)

        monkeypatch.setattr(store, "_save_entry", fail_once)
        await _receive(busy_activity, "runner", "steer", monkeypatch)
        await event._attended_activity_task
        assert attempts == 2
        reloaded = reader.lookup_by_session_key(key)
        assert reloaded is not None and reloaded.last_user_activity_at is not None
        assert reloaded.last_user_activity_at.timestamp() == pytest.approx(
            event._attended_activity_received_at,
        )
        assert entry.last_user_activity_at == reloaded.last_user_activity_at
    finally:
        for owner in (store, reader):
            await asyncio.to_thread(owner.close_all_db_handles)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["runner", "adapter"])
async def test_cancelled_receipt_does_not_retry_persistence(busy_activity, monkeypatch, path):
    runner, event, key, state = busy_activity
    entered = asyncio.Event()
    release = asyncio.Event()

    async def persist(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return True

    runner.async_session_store.mark_user_activity.side_effect = persist
    await _receive(busy_activity, path, "steer", monkeypatch)
    task = event._attended_activity_task
    await asyncio.wait_for(entered.wait(), timeout=5)
    # Shutdown cancels retained tasks that are already awaiting persistence.
    assert task in runner._background_tasks
    task.cancel()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert runner.async_session_store.mark_user_activity.await_count == 1
    assert not getattr(event, "_attended_activity_marked", False)
    assert state.conversation.last_user_activity_at == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["disabled", "no_session", "no_key", "automation", "older_receipt"])
async def test_terminal_receipt_does_not_retry(busy_activity, monkeypatch, terminal):
    runner, event, key, state = busy_activity
    load_config = Mock(return_value={} if terminal == "disabled" else CONFIG)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", load_config)
    if terminal == "no_session":
        runner.async_session_store.lookup_by_session_key.return_value = None
    elif terminal == "no_key":
        key = None
        runner._session_key_for_source = lambda _source: None
    elif terminal == "automation":
        event.internal = True
    elif terminal == "older_receipt":
        # Store False means missing entry or non-newer instant, not failed I/O.
        runner.async_session_store.mark_user_activity.return_value = False
    runner._schedule_attended_user_activity(event, event.source, key)
    task = getattr(event, "_attended_activity_task", None)
    if task is not None:
        await task
    assert load_config.call_count == (0 if terminal == "automation" else 1)
    assert runner.async_session_store.mark_user_activity.await_count == (1 if terminal == "older_receipt" else 0)


@pytest.mark.platforms("posix")
def test_turn_marker_preserves_updated_instant_during_dst_fallback(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo

    from gateway import session_lifecycle

    instant = datetime(2026, 11, 1, 1, 20, fold=1, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="dst-turn", user_id="owner")
    stores = []
    try:
        with monkeypatch.context() as local:
            local.setenv("TZ", "America/Los_Angeles")
            time.tzset()
            touched = datetime.fromtimestamp(instant)
            local.setattr(session_lifecycle, "_now", lambda: touched)
            writer = SessionStore(tmp_path / "sessions", GatewayConfig())
            stores.append(writer)
            key = writer.get_or_create_session(source).session_key
            assert writer.mark_user_activity(key, touched)
            token = writer.mark_turn_active(key)
            assert token
            reader = SessionStore(tmp_path / "sessions", GatewayConfig())
            stores.append(reader)
            entry = reader.lookup_by_session_key(key)
            assert entry is not None and entry.active_turn_token == token
            assert entry.last_user_activity_at is not None
            assert entry.last_user_activity_at.timestamp() == instant
            assert entry.updated_at.timestamp() == instant
            assert entry.updated_at.fold == 1
    finally:
        time.tzset()
        for store in stores:
            store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_result", ["false", "exception", "cancelled"])
async def test_drain_retries_failed_receipt_with_original_arrival_time(busy_activity, receipt_result):
    runner, event, key, state = busy_activity
    stamp = event._attended_activity_received_at = time.time() - 60

    async def failed_receipt():
        if receipt_result == "exception":
            raise RuntimeError("transient receipt failure")
        if receipt_result == "cancelled":
            raise asyncio.CancelledError
        return False

    task = event._attended_activity_task = asyncio.create_task(failed_receipt())
    await asyncio.gather(task, return_exceptions=True)
    assert await runner._mark_attended_user_activity(event, event.source, key)
    assert event._attended_activity_marked is True
    assert state.conversation.last_user_activity_at == stamp
    runner.async_session_store.mark_user_activity.assert_awaited_once_with(
        key, when=datetime.fromtimestamp(stamp),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["idle", "busy", "startup", "reconnect", "compression", "orphan"])
async def test_drain_arrival_records_receipt_before_spool_and_preserves_it_on_replay(
    tmp_path, monkeypatch, path,
):
    from gateway.attended_model import is_genuine_user_message
    from gateway.run_pending_recovery import _claim_recovered_followup
    from gateway.session import AsyncSessionStore
    from gateway.shutdown_flush import recover_pending_spool
    from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: CONFIG)
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    key = runner._session_key_for_source(source)
    store = SessionStore(tmp_path / "sessions", runner.config)
    reader = SessionStore(tmp_path / "sessions", runner.config)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    persist = AsyncMock(wraps=runner.async_session_store.mark_user_activity)
    runner.async_session_store.mark_user_activity = persist
    entry = store.get_or_create_session(source)
    previous = time.time() - 600
    assert store.mark_user_activity(key, datetime.fromtimestamp(previous))
    runner._session_state(key).conversation.last_user_activity_at = previous
    runner._draining = path in {"idle", "busy"}
    runner._is_user_authorized_for_source = lambda _source: True
    runner._admit_bot_message_for_source = lambda _source: True
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda event, _source: event)
    original = MessageEvent(text="human drain arrival", source=source, user_id=source.user_id)
    queued = []
    try:
        before = time.time()
        if path == "idle":
            assert not runner._is_session_running(key)
            assert await runner._handle_admitted_message(original) is None
        elif path == "busy":
            runner._session_state(key).turn.agent = Mock()
            assert await runner._handle_active_session_busy_message(original, key) is True
        elif path in {"startup", "reconnect"}:
            runner._startup_restore_in_progress = path == "startup"
            runner._reconnect_restore_keys = {key: 1} if path == "reconnect" else {}
            assert await runner._hm_admit_event(original) is None
            assert runner._startup_restore_queue == [original]
        else:
            assert await runner._hm_admit_event(original) == (original, source, False)
            if path == "compression":
                runner._session_has_compression_in_flight = AsyncMock(return_value=True)
                runner._start_compression_deferred_drain = Mock()
                assert await runner._defer_compression_locked_event(key, original)
            else:
                older = MessageEvent(text="older queued input", source=source)
                runner._rescue_orphaned_overflow = lambda *_args: older
                assert runner._hm_rescue_orphaned_fifo(original, source, False, key)[0] is older
        received = time.time()
        receipt = getattr(original, "_attended_activity_received_at", None)
        assert receipt is not None and before <= receipt <= received
        task = getattr(original, "_attended_activity_task", None)
        if path in {"startup", "reconnect", "compression", "orphan"}:
            assert task is None  # Buffers preserve metadata, not pre-admission writes.
            persist.assert_not_awaited()
        else:
            assert task is not None
            # Repeated scheduling must join the arrival task, not stamp again.
            monkeypatch.setattr("gateway.run_inbound.time.time", lambda: received + 600)
            runner._schedule_attended_user_activity(original, source, key)
            assert original._attended_activity_task is task
            await task
            restored = reader.get_last_user_activity_at(key, refresh=True)
            assert restored is not None and restored.timestamp() == pytest.approx(receipt, rel=0, abs=1e-6)
            persist.assert_awaited_once_with(key, when=datetime.fromtimestamp(receipt))
        assert adapter.sent == []
        if path in {"startup", "reconnect"}:
            assert runner._persist_shutdown_pending_messages() == 1
            assert runner._startup_restore_queue == []
        else:
            assert adapter._pending_messages[key] is original
            assert runner._persist_shutdown_pending_messages() == 1
        spool, = (tmp_path / "pending_messages").glob("*.json")
        payload = json.loads(spool.read_text())
        assert payload["data"]["_attended_activity_received_at"] == receipt
        if path in {"startup", "reconnect"}:
            assert payload["reason"] == "restore_shutdown"

        def claim(recovered_key, session_id, data, spool):
            return _claim_recovered_followup(
                runner, recovered_key, session_id, data, spool, source, source.user_id, queued,
            )

        count, held = recover_pending_spool(
            session_db=store,
            session_resolver=lambda _key, **_kwargs: (entry.session_id, store),
            deferred_followup=claim,
        )
        assert count == 1 and not held
        replay, = queued
        assert is_genuine_user_message(replay)
        assert replay._attended_activity_received_at == receipt
        monkeypatch.setattr("gateway.run_inbound.time.time", lambda: receipt + 600)
        runner._schedule_attended_user_activity(replay, source, key)
        task = replay._attended_activity_task
        runner._schedule_attended_user_activity(replay, source, key)
        assert replay._attended_activity_task is task
        await task
        assert await runner._mark_attended_user_activity(replay, source, key)
        assert replay._attended_activity_received_at == receipt
        assert runner._session_state(key).conversation.last_user_activity_at == receipt
        restored = reader.get_last_user_activity_at(key, refresh=True)
        assert restored is not None and restored.timestamp() == pytest.approx(receipt, rel=0, abs=1e-6)
        assert persist.await_count == (1 if path in {"startup", "reconnect", "compression", "orphan"} else 2)
        persist.assert_awaited_with(key, when=datetime.fromtimestamp(receipt))
    finally:
        await asyncio.gather(*list(runner._background_tasks), return_exceptions=True)
        for session_store in (store, reader):
            session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["startup", "reconnect", "compression", "orphan"])
@pytest.mark.parametrize("sender", ["unauthorized", "rejected-bot", "admitted-bot", "human"])
async def test_admission_buffers_only_publish_authorized_human_receipts(
    tmp_path, monkeypatch, path, sender,
):
    from gateway.session import AsyncSessionStore
    from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: CONFIG)
    runner, adapter = make_restart_runner()
    owner = make_restart_source(chat_type="group", thread_id="topic")
    key = runner._session_key_for_source(owner)
    store = SessionStore(tmp_path / "sessions", runner.config)
    reader = SessionStore(tmp_path / "sessions", runner.config)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    persist = AsyncMock(wraps=runner.async_session_store.mark_user_activity)
    runner.async_session_store.mark_user_activity = persist
    store.get_or_create_session(owner)
    previous = time.time() - 600
    assert store.mark_user_activity(key, datetime.fromtimestamp(previous))
    runner._session_state(key).conversation.last_user_activity_at = previous
    source = make_restart_source(chat_type="group", thread_id="topic")
    source.user_id = "stranger" if sender == "unauthorized" else owner.user_id
    source.is_bot = sender in {"rejected-bot", "admitted-bot"}
    assert runner._session_key_for_source(source) == key
    runner._is_user_authorized_for_source = Mock(side_effect=lambda source: source.user_id == owner.user_id)
    runner._admit_bot_message_for_source = Mock(return_value=sender != "rejected-bot")
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda event, _source: event)
    runner._normalize_source_for_session_key = lambda source: source
    runner._intake_adapter_for = lambda _source: adapter
    runner._startup_restore_in_progress = path == "startup"
    runner._reconnect_restore_keys = {key: 1} if path == "reconnect" else {}
    event = MessageEvent(text="shared-topic input", source=source, user_id=source.user_id)

    async def handle(replayed):
        await runner._handle_admitted_message(replayed)
        replayed._gateway_accepted = adapter._pending_messages.get(key) is replayed

    adapter.handle_message = AsyncMock(side_effect=handle)
    try:
        admitted = await runner._hm_admit_event(event)
        receipt = getattr(event, "_attended_activity_received_at", None)
        if path in {"startup", "reconnect"}:
            assert admitted is None
            assert runner._startup_restore_queue == [event]
        elif admitted is not None:
            if path == "compression":
                runner._session_has_compression_in_flight = AsyncMock(return_value=True)
                runner._start_compression_deferred_drain = Mock()
                assert await runner._defer_compression_locked_event(key, event)
            else:
                older = MessageEvent(text="older input", source=owner)
                runner._rescue_orphaned_overflow = lambda *_args: older
                assert runner._hm_rescue_orphaned_fifo(event, source, False, key)[0] is older
        await asyncio.gather(*list(runner._background_tasks))
        monkeypatch.setattr("gateway.run_inbound.time.time", lambda: previous + 1200)
        runner._draining = True
        runner._startup_restore_in_progress = False
        runner._reconnect_restore_keys = {}
        if path in {"startup", "reconnect"}:
            assert await runner._drain_startup_restore_queue() == (sender in {"human", "admitted-bot"})
            adapter.handle_message.assert_awaited_once_with(event)
            assert runner._startup_restore_queue == []
        elif admitted is not None:
            await runner._handle_admitted_message(event)
        await asyncio.gather(*list(runner._background_tasks))
        expected = receipt if sender == "human" else previous
        restored = reader.get_last_user_activity_at(key, refresh=True)
        assert restored is not None and restored.timestamp() == pytest.approx(expected, rel=0, abs=1e-6)
        assert runner._session_state(key).conversation.last_user_activity_at == expected
        if sender == "human":
            persist.assert_awaited_once_with(key, when=datetime.fromtimestamp(receipt))
        else:
            persist.assert_not_awaited()
        if sender == "unauthorized":
            runner._admit_bot_message_for_source.assert_not_called()
        assert adapter.sent == []
    finally:
        await asyncio.gather(*list(runner._background_tasks), return_exceptions=True)
        scheduler = getattr(runner, "_replay_scheduler", None)
        if scheduler is not None:
            await scheduler.close()
        for session_store in (store, reader):
            session_store.close_all_db_handles()


@pytest.mark.parametrize("receipt", [None, True, False, "123", {}, [], float("nan"), float("inf"), -float("inf"), 123, 123.5])
def test_recovered_receipt_requires_a_finite_number(receipt):
    from gateway.attended_model import is_genuine_user_message
    from gateway.run_pending_recovery import _claim_recovered_followup
    from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

    runner, _adapter = make_restart_runner()
    source = make_restart_source()
    queued = []
    data = {"text": "human input", "_attended_activity_received_at": receipt}
    assert _claim_recovered_followup(runner, "key", "session", data, None, source, source.user_id, queued)
    replay, = queued
    valid = type(receipt) in (int, float) and receipt in (123, 123.5)
    assert getattr(replay, "_attended_activity_received_at", None) == (receipt if valid else None)
    assert is_genuine_user_message(replay) is valid


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule", [False, True], ids=["turn-start", "busy-admission"])
@pytest.mark.parametrize("reason", ["shutdown_follow_up", "drain_deferred", "turn_lease_wait"])
@pytest.mark.parametrize("recorded_receipt", [True, False], ids=["original-receipt", "legacy-spool"])
@pytest.mark.parametrize("persisted_receipt", [True, False], ids=["arrival-written", "arrival-not-written"])
async def test_replayed_followup_never_counts_as_new_activity(
    busy_activity, tmp_path, monkeypatch, schedule, reason, recorded_receipt, persisted_receipt,
):
    from gateway.run_pending_recovery import _claim_recovered_followup
    from gateway.session import AsyncSessionStore
    from gateway.shutdown_flush import flush_pending_to_file, recover_pending_spool

    runner, original, key, state = busy_activity
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = SessionStore(tmp_path / "sessions", runner.config)
    reader = SessionStore(tmp_path / "sessions", runner.config)
    receipt = datetime.fromtimestamp(time.time() - 600).timestamp()
    original._attended_activity_received_at = receipt
    original._drain_deferred = reason == "drain_deferred"
    entry = store.get_or_create_session(original.source)
    assert entry.session_key == key
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    queued = []
    previous = receipt - 60
    assert store.mark_user_activity(key, datetime.fromtimestamp(previous))
    state.conversation.last_user_activity_at = previous
    expected = receipt if recorded_receipt or persisted_receipt else previous
    try:
        if persisted_receipt:
            assert await runner._mark_attended_user_activity(original, original.source, key)
        if reason == "turn_lease_wait":
            from gateway.run_turn_lease_wait import preserve_lease_wait
            preserve_lease_wait(runner, key, entry.session_id, original)
        else:
            assert flush_pending_to_file({key: original}, reason=reason) == 1
        path, = (tmp_path / "pending_messages").glob("*.json")
        if not recorded_receipt:
            payload = json.loads(path.read_text())
            payload["data"].pop("_attended_activity_received_at", None)
            # Ordering/flush time is not proof of a human arrival.
            payload["ts"] = time.time()
            path.write_text(json.dumps(payload))

        def claim(recovered_key, session_id, data, spool):
            return _claim_recovered_followup(
                runner, recovered_key, session_id, data, spool,
                original.source, original.source.user_id, queued,
            )

        count, held = recover_pending_spool(
            session_db=store,
            session_resolver=lambda _key, **_kwargs: (entry.session_id, store),
            deferred_followup=claim,
        )
        assert count == 1 and not held
        replay = queued.pop()
        if schedule:
            runner._schedule_attended_user_activity(replay, replay.source, key)
            task = getattr(replay, "_attended_activity_task", None)
            if task is not None:
                await task
        else:
            marked = await runner._mark_attended_user_activity(replay, replay.source, key)
            assert marked is recorded_receipt
        assert state.conversation.last_user_activity_at == expected
        restored = reader.get_last_user_activity_at(key, refresh=True)
        assert restored is not None and restored.timestamp() == expected
        assert getattr(replay, "_attended_activity_marked", False) is recorded_receipt
    finally:
        for session_store in (store, reader):
            session_store.close_all_db_handles()
