"""A successor admitted while its predecessor flushes must wait, not ask for a resend."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import shutdown_flush
from gateway.run_pending_recovery import _recovered_followup_event
from gateway.run_turn_lease_wait import preserve_lease_wait, retire_lease_wait
from gateway.turn_lease import SessionTurnLeaseRegistry
from tests.gateway.test_duplicate_user_message import _bootstrap, _event

# Outer wait_for bounds only catch hangs. The behavior under test is bounded by lease timeouts
# of 1 s or less, but spool writes run on a worker thread that a loaded CI runner can starve.
HANG_GUARD_SECONDS = 30


@pytest.mark.asyncio
@pytest.mark.parametrize("holder_key", ["same-key", "other-key"])
async def test_admitted_successor_waits_past_lease_deadline(monkeypatch, tmp_path, caplog, holder_key):
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._turn_leases = SessionTurnLeaseRegistry()
    event = _event()
    event.source.thread_id = "362027"
    event.source.chat_type = "dm"
    key = runner._session_key_for_source(event.source)
    actual_holder_key = key if holder_key == "same-key" else holder_key
    holder = await runner._turn_leases.acquire("sess-dedup", owner_key=actual_holder_key, generation=8)
    monkeypatch.setenv("HERMES_TURN_LEASE_TIMEOUT", "0.01")
    monkeypatch.setattr(shutdown_flush, "_get_flush_dir", lambda: tmp_path)
    runner._begin_session_run_generation = lambda _key: 9
    executed = []
    spooled = asyncio.Event()
    loop = asyncio.get_running_loop()

    def preserve(*args):
        preserve_lease_wait(*args)
        loop.call_soon_threadsafe(spooled.set)

    monkeypatch.setattr("gateway.run_turn_lease_wait.preserve_lease_wait", preserve)

    async def handle(event, source, key, generation):
        await runner._hmwa_acquire_turn_lease(
            key, generation, runner.session_store.get_or_create_session(source), None, event,
        )
        executed.append(event.message_id)
        runner.session_store.has_platform_message_id.return_value = True
        return "processed once"

    runner._handle_message_with_agent = handle
    before = time.time()
    task = asyncio.create_task(runner._handle_message(event))
    try:
        await asyncio.wait_for(spooled.wait(), HANG_GUARD_SECONDS)
        receipt = getattr(event, "_attended_activity_received_at", None)
        assert receipt is not None and before <= receipt <= time.time()
        assert not task.done(), "the admitted message was rejected at the lease deadline"
        assert not executed
        payload = json.loads(event._gateway_lease_wait_spool.read_text())
        assert payload["data"]["_attended_activity_received_at"] == receipt
        assert payload["data"]["message_id"] == event.message_id
        assert payload["data"]["text"] == event.text
        expected = "same-key generation overlap" if holder_key == "same-key" else "two routing keys"
        assert expected in caplog.text
        if holder_key == "same-key":
            assert "two routing keys" not in caplog.text
        runner._turn_leases.release(holder)
        assert await asyncio.wait_for(task, HANG_GUARD_SECONDS) == "processed once"
        assert executed == [event.message_id]
        assert not event._gateway_lease_wait_spool.exists()
    finally:
        runner._turn_leases.release(holder)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_middle", [False, True])
async def test_extended_lease_wait_preserves_fifo_without_detached_acquires(cancel_middle):
    registry = SessionTurnLeaseRegistry(max_entries=1)
    holder = await registry.acquire("session", owner_key="topic", generation=8)
    queued = [asyncio.Event() for _ in range(3)]
    order = []

    async def successor(index):
        async def preserved():
            queued[index].set()

        token = await registry.acquire(
            "session", owner_key="topic", generation=9 + index,
            timeout=0.01, on_timeout=preserved,
        )
        order.append(index)
        registry.release(token)

    tasks = []
    try:
        for index in range(3):
            tasks.append(asyncio.create_task(successor(index)))
            await asyncio.wait_for(queued[index].wait(), HANG_GUARD_SECONDS)
        assert not order
        # Capacity pressure must not evict a held lease with queued waiters.
        independent = await registry.acquire("unrelated", owner_key="other", generation=1)
        registry.release(independent)
        if cancel_middle:
            tasks[1].cancel()
            await asyncio.gather(tasks[1], return_exceptions=True)
        registry.release(holder)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), HANG_GUARD_SECONDS)
        assert order == ([0, 2] if cancel_middle else [0, 1, 2])
        next_turn = await registry.acquire("session", owner_key="topic", generation=12, timeout=0.02)
        assert registry.release(next_turn)
        assert registry._leases["session"].idle
    finally:
        registry.release(holder)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", ["msg-42", None])
async def test_cancelled_waiter_has_replayable_identity_and_media(monkeypatch, tmp_path, message_id):
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._turn_leases = SessionTurnLeaseRegistry()
    event = _event()
    event.message_id = message_id
    event.source.thread_id = "362027"
    event.media_urls = ["file:///retained-voice.ogg"]
    event.media_types = ["audio/ogg"]
    key = runner._session_key_for_source(event.source)
    holder = await runner._turn_leases.acquire("sess-dedup", owner_key=key, generation=8)
    monkeypatch.setenv("HERMES_TURN_LEASE_TIMEOUT", "0.01")
    monkeypatch.setattr(shutdown_flush, "_get_flush_dir", lambda: tmp_path)
    spooled = asyncio.Event()
    loop = asyncio.get_running_loop()

    def preserve(*args):
        preserve_lease_wait(*args)
        loop.call_soon_threadsafe(spooled.set)

    monkeypatch.setattr("gateway.run_turn_lease_wait.preserve_lease_wait", preserve)
    entry = runner.session_store.get_or_create_session(event.source)
    task = asyncio.create_task(runner._hmwa_acquire_turn_lease(key, 9, entry, None, event))
    try:
        await asyncio.wait_for(spooled.wait(), HANG_GUARD_SECONDS)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        runner._turn_leases.release(holder)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    path = event._gateway_lease_wait_spool
    assert path.exists()
    if message_id is None:
        assert event._hermes_recovery_id.startswith("hermes-recovery-")
    claimed = []
    db = MagicMock()

    def claim(_key, _session_id, data, spool):
        assert data["drain_deferred"] is True
        assert data["shutdown_follow_up"] is True
        assert spool == path
        claimed.append(_recovered_followup_event(data, event.source, data["source_user_id"]))
        return True

    assert shutdown_flush.recover_pending_to_db(
        db, session_resolver=lambda *_a, **_kw: ("sess-dedup", db), deferred_followup=claim,
    ) == 1
    assert path.exists(), "startup must retain the only copy until replay accepts it"
    db.append_message.assert_not_called()
    assert len(claimed) == 1
    recovered = claimed[0]
    assert recovered.message_id == message_id
    assert recovered.text == event.text
    assert recovered.media_urls == event.media_urls
    assert recovered.media_types == event.media_types
    assert recovered.source.user_id == event.source.user_id
    assert recovered.source.thread_id == "362027"
    if message_id is None:
        assert recovered._hermes_recovery_id == event._hermes_recovery_id
    # Never retire the only copy on a failed/unknown transcript write.
    retire_lease_wait(runner, event)
    assert path.exists()
    runner.session_store.has_platform_message_id.return_value = True
    retire_lease_wait(runner, event)
    assert not path.exists()


@pytest.mark.asyncio
async def test_agent_cleanup_can_admit_next_generation_while_outer_lease_is_held(monkeypatch, tmp_path):
    runner = _bootstrap(monkeypatch, tmp_path)
    # Restore actual generation management instead of the persistence fixture's shortcuts.
    del runner._begin_session_run_generation
    del runner._is_session_run_current
    runner._persist_active_agents = MagicMock()
    registry = runner._turn_leases = SessionTurnLeaseRegistry()
    key = runner._session_key_for_source(_event().source)
    state = runner._session_state(key)
    state.persistent.run_generation = 7
    assert runner._begin_session_run_generation(key) == 8
    state.turn.agent = object()
    holder = await registry.acquire("sess-dedup", owner_key=key, generation=8)
    state.turn.lease_tokens[8] = holder
    tracking = asyncio.create_task(asyncio.Event().wait())
    context = SimpleNamespace(
        session_key=key, run_generation=8,
        stream_consumer_holder=[None], streaming_tts_consumer_holder=[None],
    )
    try:
        await runner._run_agent_cleanup_turn_tasks(
            context, progress_task=None, log_task=None, interrupt_monitor=None,
            _notify_task=None, tracking_task=tracking, stream_task=None,
        )
        assert not runner._is_session_running(key)
        assert holder.lease.lock.locked()
        assert state.turn.lease_tokens[8] is holder
        assert runner._begin_session_run_generation(key) == 9
        # This is a normal cleanup window, not 2 routing keys or lease eviction.
        assert holder.owner_key == key and holder.generation == 8
    finally:
        tracking.cancel()
        await asyncio.gather(tracking, return_exceptions=True)
        runner._release_turn_lease(key, 8)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", [
    "stop", "stop-during-shutdown", "drop", "drop-during-shutdown", "shutdown",
])
async def test_wait_spool_follows_inbound_exit_intent(monkeypatch, tmp_path, exit_kind):
    from tests.gateway.test_stop_pauses_goal_and_holds_wakes import _runner

    runner, adapter, source, key, session_id = await _runner(monkeypatch)
    runner._mark_attended_user_activity = AsyncMock(return_value=False)
    runner._run_post_turn_hooks = AsyncMock()
    adapter._message_handler = runner._handle_stop_command
    event = _event()
    event.source = source
    holder = await runner._turn_leases.acquire(session_id, owner_key=key, generation=8)
    monkeypatch.setenv("HERMES_TURN_LEASE_TIMEOUT", "0.01")
    monkeypatch.setattr(shutdown_flush, "_get_flush_dir", lambda: tmp_path)
    spooled = asyncio.Event()
    loop = asyncio.get_running_loop()

    def preserve(*args):
        preserve_lease_wait(*args)
        loop.call_soon_threadsafe(spooled.set)

    monkeypatch.setattr("gateway.run_turn_lease_wait.preserve_lease_wait", preserve)

    async def handle(event, source, key, generation):
        entry = await runner.async_session_store.get_or_create_session(source)
        await runner._hmwa_acquire_turn_lease(key, generation, entry, None, event)
        # Deliberate rejection before the user row.

    runner._handle_message_with_agent = handle
    task = asyncio.create_task(runner._handle_message(event))
    adapter._session_tasks[key] = task
    adapter._active_sessions[key] = asyncio.Event()
    try:
        await asyncio.wait_for(spooled.wait(), HANG_GUARD_SECONDS)
        path = event._gateway_lease_wait_spool
        runner._draining = exit_kind.endswith("during-shutdown")
        if exit_kind.startswith("stop"):
            stop = _event()
            stop.source, stop.text = source, "/stop"
            await adapter._dispatch_active_session_command(stop, key, "stop")
        elif exit_kind == "shutdown":
            runner._draining = True
            task.cancel()
        else:
            runner._turn_leases.release(holder)
        await asyncio.gather(task, return_exceptions=True)
        assert path.exists() is (exit_kind == "shutdown")
        assert (await runner.async_session_store.lookup_by_session_key(key)).session_id == session_id
    finally:
        runner._turn_leases.release(holder)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reconnect_does_not_decline_a_live_durable_lease_wait(monkeypatch, tmp_path):
    from gateway.run import _AGENT_PENDING_SENTINEL
    from gateway.run_pending_recovery import _defer_followup
    from tests.gateway.test_stop_pauses_goal_and_holds_wakes import _runner

    runner, _adapter, source, key, session_id = await _runner(monkeypatch)
    runner._session_state(key).turn.agent = _AGENT_PENDING_SENTINEL
    event = _event()
    event.source = source
    data = shutdown_flush._serialise_value(event)
    data.update(drain_deferred=True, shutdown_follow_up=True, _recovery_reason="turn_lease_wait")
    monkeypatch.setattr("gateway.run_pending_recovery.input_already_durable", lambda *a: True)
    recovered = []
    outcome = _defer_followup(
        runner, {}, source.platform, key, session_id, data, tmp_path / "pending.json",
        reconnect_recovery=True, recovered_events=recovered,
    )
    assert outcome is None
    assert recovered == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["serialization", "home", "write"])
async def test_spool_failure_returns_resend_notice_and_refunds_tick(monkeypatch, tmp_path, failure):
    from agent.i18n import t

    runner = _bootstrap(monkeypatch, tmp_path)
    runner._release_unsettled_loop_tick = AsyncMock()
    event = _event()
    key = runner._session_key_for_source(event.source)
    holder = await runner._turn_leases.acquire("sess-dedup", owner_key=key, generation=8)
    monkeypatch.setenv("HERMES_TURN_LEASE_TIMEOUT", "0.01")
    if failure == "serialization":
        monkeypatch.setattr(shutdown_flush, "_serialise_value", lambda *a: None)
    elif failure == "home":
        monkeypatch.setattr("gateway.run_turn_lease_wait.pending_home_for_key", lambda *a: None)
    else:
        def fail(*a):
            raise OSError("disk full")
        monkeypatch.setattr(shutdown_flush, "_write_payload", fail)

    async def handle(event, source, key, generation):
        await runner._hmwa_acquire_turn_lease(
            key, generation, runner.session_store.get_or_create_session(source), None, event,
        )
        pytest.fail("an unpreserved message acquired the held lease")

    runner._handle_message_with_agent = handle
    try:
        assert await runner._handle_message(event) == t("gateway.busy.another_turn_running")
        runner._release_unsettled_loop_tick.assert_awaited_once_with(event, ran=False)
        assert holder.lease.holder is holder
    finally:
        runner._turn_leases.release(holder)
    next_turn = await runner._turn_leases.acquire("sess-dedup", owner_key=key, generation=10)
    assert runner._turn_leases.release(next_turn)


def test_recovered_followup_reuses_its_only_spool_on_another_lease_wait(monkeypatch, tmp_path):
    runner = _bootstrap(monkeypatch, tmp_path)
    event = _event()
    key = runner._session_key_for_source(event.source)
    monkeypatch.setattr(shutdown_flush, "_get_flush_dir", lambda: tmp_path)
    preserve_lease_wait(runner, key, "sess-dedup", event)
    original = event._gateway_lease_wait_spool
    data = json.loads(original.read_text())["data"]
    recovered = _recovered_followup_event(data, event.source, event.source.user_id)
    recovered._hermes_recovery_spool = original
    preserve_lease_wait(runner, key, "sess-dedup", recovered)
    assert recovered._gateway_lease_wait_spool == original
    assert list(tmp_path.glob("pending-*.json")) == [original]


@pytest.mark.asyncio
async def test_stuck_predecessor_has_bounded_visible_failure(monkeypatch, tmp_path):
    from agent.i18n import t

    runner = _bootstrap(monkeypatch, tmp_path)
    runner._release_unsettled_loop_tick = AsyncMock()
    event = _event()
    key = runner._session_key_for_source(event.source)
    holder = await runner._turn_leases.acquire("sess-dedup", owner_key=key, generation=8)
    monkeypatch.setenv("HERMES_TURN_LEASE_TIMEOUT", "0.01")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {
        "gateway": {"turn_lease_max_wait_seconds": 1},
    })
    monkeypatch.setattr(shutdown_flush, "_get_flush_dir", lambda: tmp_path)

    async def handle(event, source, key, generation):
        await runner._hmwa_acquire_turn_lease(
            key, generation, runner.session_store.get_or_create_session(source), None, event,
        )
        pytest.fail("the stuck predecessor lost mutual exclusion")

    runner._handle_message_with_agent = handle
    try:
        reply = await asyncio.wait_for(runner._handle_message(event), HANG_GUARD_SECONDS)
        assert reply == t("gateway.busy.another_turn_running")
        runner._release_unsettled_loop_tick.assert_awaited_once_with(event, ran=False)
        assert not event._gateway_lease_wait_spool.exists()
        assert holder.lease.holder is holder
    finally:
        runner._turn_leases.release(holder)


@pytest.mark.parametrize("config", [
    {"gateway": None},
    {"gateway": "not-a-mapping"},
    {"gateway": {"turn_lease_max_wait_seconds": None}},
    {},
])
def test_lease_wait_max_seconds_tolerates_empty_or_malformed_gateway_config(monkeypatch, config):
    # The shipped template leaves ``gateway:`` with only commented children, which loads as None.
    from gateway.run_turn_lease_wait import lease_wait_max_seconds
    from gateway.turn_lease import DEFAULT_MAX_LEASE_WAIT

    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: config)
    assert lease_wait_max_seconds() == DEFAULT_MAX_LEASE_WAIT


def test_lease_wait_max_seconds_survives_config_load_failure(monkeypatch):
    from gateway.run_turn_lease_wait import lease_wait_max_seconds
    from gateway.turn_lease import DEFAULT_MAX_LEASE_WAIT

    def boom():
        raise OSError("config unreadable")

    monkeypatch.setattr("gateway.run._load_gateway_config", boom)
    assert lease_wait_max_seconds() == DEFAULT_MAX_LEASE_WAIT


@pytest.mark.asyncio
async def test_uncontended_turn_runs_with_null_gateway_config(monkeypatch, tmp_path):
    runner = _bootstrap(monkeypatch, tmp_path)
    event = _event()
    key = runner._session_key_for_source(event.source)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"gateway": None})
    await runner._hmwa_acquire_turn_lease(
        key, 1, runner.session_store.get_or_create_session(event.source), None, event,
    )
    token = runner._session_state(key).turn.lease_tokens.pop(1)
    runner._turn_leases.release(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_ack", [False, True])
async def test_long_lease_wait_sends_one_busy_ack(monkeypatch, tmp_path, prior_ack):
    from tests.gateway.test_stop_pauses_goal_and_holds_wakes import _Adapter

    runner = _bootstrap(monkeypatch, tmp_path)
    adapter = _Adapter()
    runner._delivery_adapter_for = lambda source: adapter
    event = _event()
    key = runner._session_key_for_source(event.source)
    holder = await runner._turn_leases.acquire("sess-dedup", owner_key=key, generation=8)
    monkeypatch.setenv("HERMES_TURN_LEASE_TIMEOUT", "0.01")
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "true")
    monkeypatch.setattr("gateway.turn_lease.DEFAULT_LEASE_NOTICE_DELAY", 0.02)
    from gateway.run_turn_lease_wait import lease_wait_notice

    noticed = asyncio.Event()

    async def notice(*args):
        await lease_wait_notice(*args)
        noticed.set()

    monkeypatch.setattr("gateway.run_turn_lease_wait.lease_wait_notice", notice)
    monkeypatch.setattr(shutdown_flush, "_get_flush_dir", lambda: tmp_path)
    if prior_ack:
        await runner._send_busy_ack_reply(event, adapter, "Already queued")

    async def handle(event, source, key, generation):
        await runner._hmwa_acquire_turn_lease(
            key, generation, runner.session_store.get_or_create_session(source), None, event,
        )

    runner._handle_message_with_agent = handle
    task = asyncio.create_task(runner._handle_message(event))
    try:
        await asyncio.wait_for(noticed.wait(), HANG_GUARD_SECONDS)
        assert not task.done()
        await lease_wait_notice(runner, key, event)
        assert len(adapter.sent) == 1
        assert adapter.sent[0] == ("Already queued" if prior_ack else "Queued behind the previous turn.")
        runner._turn_leases.release(holder)
        await asyncio.wait_for(task, HANG_GUARD_SECONDS)
    finally:
        runner._turn_leases.release(holder)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("draining", [False, True])
async def test_cancel_during_spool_write_settles_before_retirement(monkeypatch, tmp_path, draining):
    runner = _bootstrap(monkeypatch, tmp_path)
    event = _event()
    key = runner._session_key_for_source(event.source)
    holder = await runner._turn_leases.acquire("sess-dedup", owner_key=key, generation=8)
    monkeypatch.setenv("HERMES_TURN_LEASE_TIMEOUT", "0.01")
    monkeypatch.setattr(shutdown_flush, "_get_flush_dir", lambda: tmp_path)
    started, spooled = asyncio.Event(), asyncio.Event()
    unblock = threading.Event()
    loop = asyncio.get_running_loop()

    def preserve(*args):
        loop.call_soon_threadsafe(started.set)
        assert unblock.wait(3)
        preserve_lease_wait(*args)
        loop.call_soon_threadsafe(spooled.set)

    monkeypatch.setattr("gateway.run_turn_lease_wait.preserve_lease_wait", preserve)

    async def handle(event, source, key, generation):
        await runner._hmwa_acquire_turn_lease(
            key, generation, runner.session_store.get_or_create_session(source), None, event,
        )

    runner._handle_message_with_agent = handle
    task = asyncio.create_task(runner._handle_message(event))
    try:
        await asyncio.wait_for(started.wait(), HANG_GUARD_SECONDS)
        runner._draining = draining
        task.cancel()
        await asyncio.wait({task}, timeout=0.05)
        unblock.set()
        await asyncio.wait_for(spooled.wait(), HANG_GUARD_SECONDS)
        await asyncio.gather(task, return_exceptions=True)
        assert event._gateway_lease_wait_spool.exists() is draining
    finally:
        unblock.set()
        runner._turn_leases.release(holder)
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_spool_failure_does_not_run_unserialized_or_leak_waiter(monkeypatch, tmp_path):
    registry = SessionTurnLeaseRegistry()
    holder = await registry.acquire("session", owner_key="topic", generation=8)

    async def cannot_preserve():
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        await registry.acquire("session", owner_key="topic", generation=9, timeout=0.01,
                               on_timeout=cannot_preserve)
    assert holder.lease.holder is holder
    registry.release(holder)
    successor = await registry.acquire("session", owner_key="topic", generation=10, timeout=0.02)
    assert registry.release(successor)
