"""Native persisted loop claims, real adapter admission and explicit conversation reset.

Only the model/transport are controlled. SessionStore, metadata stores, public
/new handler, turn entry and the gateway's existing watcher are exercised.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evals.heartbeat_idle_wire import WireAdapter
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_cli import goals, loops


@pytest.fixture
def control_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    goals._get_session_db()  # initialise in sync context, not on the event loop
    yield tmp_path
    goals._DB_CACHE.clear()
    from hermes_state_registry import close_all_under
    close_all_under(tmp_path)


def native_runner(home):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = SessionStore(home / "sessions", runner.config)
    runner._running_agents = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner._background_tasks = set()
    runner._queued_events = {}
    runner._is_telegram_topic_lane = lambda source: False
    runner._cache_session_source = lambda *args: None
    runner._clear_session_env = lambda tokens: None
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="loop-recovery", user_id="owner", chat_type="dm")
    entry = runner.session_store.get_or_create_session(source)
    return runner, adapter, source, entry


def create_loop(entry, source):
    mgr = loops.LoopManager(entry.session_id)
    mgr.set("check disposable work", interval_seconds=60, times=2, route={
        "platform": "telegram", "chat_id": source.chat_id,
        "chat_type": "dm", "user_id": source.user_id,
    })
    return mgr


async def scan(runner, sid):
    state = loops.load_loop(sid)
    await runner._loop_wakeup_fire_one(sid, state, time.time() + 86400, set())


@pytest.mark.asyncio
async def test_refused_adapter_admission_releases_unstarted_claim(control_home):
    runner, adapter, source, entry = native_runner(control_home)
    mgr = create_loop(entry, source)
    # Real BasePlatformAdapter: with no message handler admission is refused.
    await scan(runner, entry.session_id)
    saved = loops.load_loop(entry.session_id)
    assert not saved.awaiting_response
    assert saved.ticks_fired == 0
    runner.session_store.close_all_db_handles()


def test_original_claim_stays_not_due_until_reconciled(control_home):
    _runner, _adapter, source, entry = native_runner(control_home)
    mgr = create_loop(entry, source)
    assert mgr.fire_tick()
    later = loops.LoopManager(entry.session_id)
    assert later.state.awaiting_response is True
    assert later.is_due(time.time() + 86400) is False


@pytest.mark.asyncio
async def test_legacy_claim_with_unknown_effect_is_parked_not_replayed(control_home):
    runner, adapter, source, entry = native_runner(control_home)
    mgr = create_loop(entry, source)
    assert mgr.fire_tick()
    # Simulate the installed pre-recovery row: it has awaiting_response but no
    # phase proving that injection never reached execution.
    import json
    raw = json.loads(mgr.state.to_json())
    raw.pop("claim_phase", None)
    runner._run_in_executor_with_context = asyncio.to_thread
    db = goals._get_session_db()
    db.set_meta("loop:" + entry.session_id, json.dumps(raw))
    await scan(runner, entry.session_id)
    parked = loops.load_loop(entry.session_id)
    assert parked.status == "paused"
    assert parked.awaiting_response is False
    assert "outcome unknown" in parked.paused_reason
    assert not adapter._session_tasks
    runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_restart_reconciles_unstarted_tick_without_replaying_it(control_home):
    runner, adapter, source, entry = native_runner(control_home)
    mgr = create_loop(entry, source)
    assert mgr.fire_tick()  # process exits between claim and adapter admission
    runner.session_store.close_all_db_handles()
    recovered, adapter, _, recovered_entry = native_runner(control_home)
    assert recovered_entry.session_id == entry.session_id
    await scan(recovered, entry.session_id)
    saved = loops.load_loop(entry.session_id)
    assert not saved.awaiting_response
    assert saved.status == "active"
    assert saved.ticks_fired == 0
    assert not adapter._session_tasks  # reconciliation itself never injects a turn
    recovered.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["running", "admitted", "queued", "recovering"])
async def test_live_or_restart_owner_keeps_claim_even_a_day_later(control_home, owner):
    runner, adapter, source, entry = native_runner(control_home)
    mgr = create_loop(entry, source)
    assert mgr.fire_tick()
    key = entry.session_key
    if owner == "running":
        runner._running_agents[key] = object()
    elif owner == "admitted":
        adapter._active_sessions[key] = object()
    elif owner == "queued":
        runner._enqueue_fifo(key, MessageEvent(text="queued", source=source), adapter)
    else:
        runner.session_store.mark_resume_pending(key, reason="restart_timeout")
    await scan(runner, entry.session_id)
    saved = loops.load_loop(entry.session_id)
    assert saved.awaiting_response and saved.ticks_fired == 1
    runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_recovery_write_boundary_rechecks_live_owner(control_home):
    runner, _adapter, source, entry = native_runner(control_home)
    mgr = create_loop(entry, source)
    assert mgr.fire_tick()
    before = loops.load_loop(entry.session_id)
    assert before is not None
    injections = []

    async def worker(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "<lambda>":
            runner._running_agents[entry.session_key] = object()
            injections.append("live owner appears at recovery worker boundary")
        return await asyncio.to_thread(func, *args, **kwargs)

    runner._run_in_executor_with_context = worker
    await scan(runner, entry.session_id)
    after = loops.load_loop(entry.session_id)
    assert after is not None
    assert injections
    assert after.awaiting_response and after.ticks_fired == before.ticks_fired
    runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["claimed", "executing", "legacy"])
@pytest.mark.parametrize("response", ["Answer to an unrelated user question", "LOOP_COMPLETE", ""])
async def test_post_turn_requires_exact_loop_wake_identity(control_home, phase, response):
    runner, adapter, source, entry = native_runner(control_home)
    try:
        mgr = create_loop(entry, source)
        assert mgr.fire_tick()
        state = mgr.state
        assert state is not None
        tick = (state.created_at, state.ticks_fired)
        if phase == "executing":
            assert mgr.start_tick(tick)
        elif phase == "legacy":
            state.claim_phase = ""
            loops.save_loop(entry.session_id, state)
        db = goals._get_session_db()
        assert db is not None
        before = db.get_meta("loop:" + entry.session_id)
        # The same session may answer a human or unrelated autonomous event while
        # an interrupted wake is still claimed. Text (even LOOP_COMPLETE) is not identity.
        for internal, metadata in [
            (False, {}),
            (True, {}),
            (True, {"gateway_loop_tick": [tick[0]]}),
            (True, {"gateway_loop_tick": [tick[0], tick[1] + 1]}),
            (True, {"gateway_loop_tick": [tick[0] + 1, tick[1]]}),
        ]:
            event = MessageEvent(text="unrelated turn", source=source, internal=internal, metadata=metadata)
            await runner._run_post_turn_hooks(
                agent_result=response, source=source, is_internal=internal, event=event,
            )
            assert db.get_meta("loop:" + entry.session_id) == before
        await runner._post_turn_loop_completion(
            session_entry=entry, source=None, final_response=response, loop_tick=None,
        )
        assert db.get_meta("loop:" + entry.session_id) == before
        # The owning wake still settles, including silent or interrupted replies.
        event = MessageEvent(
            text="loop wake", source=source, internal=True,
            metadata={"gateway_loop_tick": list(tick)},
        )
        await runner._run_post_turn_hooks(
            agent_result=response, source=source, is_internal=True, event=event,
        )
        saved = loops.load_loop(entry.session_id)
        assert saved is not None
        assert not saved.awaiting_response
        assert saved.ticks_fired == tick[1]
        assert saved.status == ("done" if response == "LOOP_COMPLETE" else "active")
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_public_new_retires_goal_and_claim_without_carryover(control_home):
    runner, adapter, source, entry = native_runner(control_home)
    old = entry.session_id
    mgr = create_loop(entry, source)
    assert mgr.fire_tick()
    goals.GoalManager(old).set("old standing responsibility")
    runner._session_db = None
    runner._cleanup_old_agent_for_reset = AsyncMock()
    runner._reset_notice_session_info = lambda source: ""
    runner._telegram_topic_new_header = lambda source: ""
    await runner._handle_reset_command(MessageEvent(text="/new", source=source))
    new = runner.session_store.peek_session_id(entry.session_key)
    assert new != old
    assert loops.load_loop(old).status == "cleared"
    assert not loops.load_loop(old).awaiting_response
    assert not goals.GoalManager(old).has_goal()
    assert not goals.GoalManager(new).has_goal()
    assert loops.load_loop(new) is None
    await scan(runner, old)
    assert not adapter._session_tasks
    runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_reset_after_admission_does_not_execute_old_tick(control_home):
    runner, adapter, source, entry = native_runner(control_home)
    mgr = create_loop(entry, source)
    executions = []
    runner._hmwa_prepare_turn = AsyncMock(return_value=(runner._PreparedTurn([], "", "old work", "old work", None, None), {}))
    async def model(**kwargs):
        executions.append(kwargs["session_id"])
        raise asyncio.CancelledError
    runner._run_agent = model
    adapter.set_message_handler(lambda event: runner._handle_message_with_agent(event, source, entry.session_key, 1))
    await scan(runner, entry.session_id)
    assert entry.session_key in adapter._session_tasks
    runner.session_store.reset_session(entry.session_key)
    await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
    assert executions == []
    runner.session_store.close_all_db_handles()
