"""Busy replies refresh attended routing at receipt, never when their queue drains."""
import asyncio
import threading
import time
from datetime import datetime, timezone
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
    if path == "adapter_fallthrough":
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
        if path == "adapter_fallthrough":
            assert result is False
    elif path == "command":
        event.text = "/status"
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
BUSY_PATHS += [("grace", "interrupt"), ("adapter_fallthrough", "queue"),
               ("foreign_accept", "queue"), ("foreign_reject", "queue"), ("command", "queue")]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,mode", BUSY_PATHS)
@pytest.mark.parametrize("kind", ["human", "relay", "goal", "process"])
async def test_only_human_receipts_refresh_once_without_blocking(busy_activity, monkeypatch, path, mode, kind):
    runner, event, key, state = busy_activity
    if kind == "relay":
        event.text = "[relay from=hermes:default/other]\nFYI"
        event.metadata = {"notification_origin": "relay_delivery"}
    elif kind == "goal":
        event.internal = True
    elif kind == "process":
        event.metadata = {"kind": "process_notification"}
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
        key, when=datetime.fromtimestamp(stamp, timezone.utc),
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
    old = datetime.fromtimestamp(1, timezone.utc)
    await asyncio.to_thread(owner_store.mark_user_activity, key, old)
    writer = SessionStore(tmp_path / "sessions", runner.config) if foreign else owner_store
    runner.session_store = writer
    # Store hydration and receipt persistence both go through the real AsyncSessionStore.
    before = time.time()
    await _receive(busy_activity, "runner", "steer", monkeypatch)
    await event._attended_activity_task
    first = await asyncio.to_thread(writer.get_last_user_activity_at, key)
    assert first is not None and first.timestamp() >= before
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
