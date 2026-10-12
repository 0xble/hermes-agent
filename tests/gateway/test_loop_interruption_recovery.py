"""Native persisted loop claims, real adapter admission and explicit conversation reset.

Only the model/transport are controlled. SessionStore, metadata stores, public
/new handler, turn entry and the gateway's existing watcher are exercised.
"""
import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import hermes_loop_guard
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
async def test_due_loop_cannot_answer_live_human_clarification(control_home):
    from tools import clarify_gateway

    runner, adapter, source, entry = native_runner(control_home)
    create_loop(entry, source)
    pending = clarify_gateway.register("loop-owner-question", entry.session_key, "What should I do?", None)
    owner = asyncio.create_task(asyncio.Event().wait())
    adapter._active_sessions[entry.session_key] = asyncio.Event()
    adapter._session_tasks[entry.session_key] = owner
    runner._running_agents[entry.session_key] = object()
    adapter.set_message_handler(
        lambda event: runner._hm_pending_reply_intercepts(event, source, entry.session_key)
    )
    try:
        await scan(runner, entry.session_id)
        assert not pending.event.is_set(), "a synthetic loop wake answered the human clarification"
        saved = loops.load_loop(entry.session_id)
        assert not saved.awaiting_response and saved.ticks_fired == 0
        assert adapter._session_tasks[entry.session_key] is owner and not owner.done()
        # A real human answer still reaches the native adapter's inline clarify path.
        await adapter.handle_message(MessageEvent(text="Use the blue option", source=source))
        assert clarify_gateway.wait_for_response(pending.clarify_id, 1) == "Use the blue option"
    finally:
        clarify_gateway.resolve_gateway_clarify(pending.clarify_id, "cleanup")
        clarify_gateway.wait_for_response(pending.clarify_id, 1)
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["/learn reusable checking", "/plan disposable work", "/start"])
async def test_native_loop_command_settles_only_after_its_dispatch_path(control_home, prompt):
    runner, adapter, source, entry = native_runner(control_home)
    mgr = loops.LoopManager(entry.session_id)
    mgr.set(prompt, interval_seconds=60, times=2, route={
        "platform": "telegram", "chat_id": source.chat_id,
        "chat_type": "dm", "user_id": source.user_id,
    })
    runner._send_command_ack = AsyncMock()  # controlled outbound transport
    runner._hmwa_prepare_turn = AsyncMock(return_value=(
        runner._PreparedTurn([], "", "rewritten work", "rewritten work", None, None), {},
    ))
    executions = []

    async def model(**kwargs):
        saved = await asyncio.to_thread(loops.load_loop, entry.session_id)
        executions.append((saved.awaiting_response, saved.claim_phase, saved.ticks_fired))
        raise asyncio.CancelledError  # stop at the real model-entry fence

    runner._run_agent = model

    async def dispatch(event):
        handled, result = await runner._hm_dispatch_idle_commands(event, source, entry.session_key)
        if handled:
            return result
        generation = runner._begin_session_run_generation(entry.session_key)
        try:
            return await runner._handle_message_with_agent(event, source, entry.session_key, generation)
        finally:
            await runner._run_post_turn_hooks(
                agent_result="LOOP_COMPLETE", source=source, is_internal=True, event=event,
            )

    adapter.set_message_handler(dispatch)
    state = loops.load_loop(entry.session_id)
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    try:
        await runner._loop_wakeup_fire_one(entry.session_id, state, time.time() + 86400, set())
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
    finally:
        hermes_loop_guard.set_loop_thread(None)
    try:
        assert executions == ([] if prompt == "/start" else [(True, "executing", 1)])
        saved = loops.load_loop(entry.session_id)
        assert not saved.awaiting_response and saved.ticks_fired == 1
        assert saved.status == "active"  # cancellation/silent command schedules the next tick
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["start_tick", "_exact_executing_tick"])
async def test_command_loop_fence_rechecks_reset_after_start_tick(control_home, boundary):
    runner, _adapter, source, entry = native_runner(control_home)
    runner.config.quick_commands = {"effect": {"type": "exec", "command": "echo effect"}}
    mgr = loops.LoopManager(entry.session_id)
    mgr.set("/effect", interval_seconds=60, times=2, route={
        "platform": "telegram", "chat_id": source.chat_id,
        "chat_type": "dm", "user_id": source.user_id,
    })
    assert mgr.fire_tick()
    saved = loops.load_loop(entry.session_id)
    event = runner._synthetic_prompt_event(source, "/effect", internal=True, reply_expected=False)
    event.metadata.update({
        "hermes_loop_wakeup": True,
        "gateway_session_id": entry.session_id,
        "gateway_loop_tick": [saved.created_at, saved.ticks_fired],
    })
    effects = []

    async def effect(command, exec_cmd):
        effects.append((command, exec_cmd))
        return "effect ran"

    runner._hm_run_exec_quick_command = effect
    runner._session_db = None
    runner._cleanup_old_agent_for_reset = AsyncMock()
    runner._reset_notice_session_info = lambda source: ""
    runner._telegram_topic_new_header = lambda source: ""
    original_executor = runner._run_in_executor_with_context
    reset = MessageEvent(text="/new", source=source)
    reset_done = False

    async def executor(func, *args, **kwargs):
        nonlocal reset_done
        result = await original_executor(func, *args, **kwargs)
        if getattr(func, "__name__", "") == boundary and not reset_done:
            reset_done = True
            await runner._handle_reset_command(reset)
        return result

    runner._run_in_executor_with_context = executor
    try:
        handled, result = await runner._hm_dispatch_idle_commands(event, source, entry.session_key)
        assert reset_done, "test did not reach the requested executor-return boundary"
        assert (handled, result) == (True, None)
        assert effects == [], "old loop command ran after public /new won the executor-return gap"
        assert loops.load_loop(entry.session_id).status == "cleared"
        assert runner.session_store.peek_session_id(entry.session_key) != entry.session_id
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["_start_owned_loop_tick", "_exact_executing_tick"])
async def test_model_loop_fence_rechecks_reset_after_start_tick(control_home, boundary):
    runner, adapter, source, entry = native_runner(control_home)
    create_loop(entry, source)
    executions = []
    runner._hmwa_prepare_turn = AsyncMock(return_value=(
        runner._PreparedTurn([], "", "old work", "old work", None, None), {},
    ))

    async def model(**kwargs):
        executions.append(kwargs["session_id"])
        raise asyncio.CancelledError

    runner._run_agent = model
    runner._session_db = None
    runner._cleanup_old_agent_for_reset = AsyncMock()
    runner._reset_notice_session_info = lambda source: ""
    runner._telegram_topic_new_header = lambda source: ""
    original_executor = runner._run_in_executor_with_context
    reset = MessageEvent(text="/new", source=source)
    reset_done = False

    async def executor(func, *args, **kwargs):
        nonlocal reset_done
        result = await original_executor(func, *args, **kwargs)
        if getattr(func, "__name__", "") == boundary and not reset_done:
            reset_done = True
            await runner._handle_reset_command(reset)
        return result

    runner._run_in_executor_with_context = executor
    generation = runner._begin_session_run_generation(entry.session_key)
    adapter.set_message_handler(
        lambda event: runner._handle_message_with_agent(event, source, entry.session_key, generation)
    )
    try:
        await scan(runner, entry.session_id)
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        assert reset_done, "test did not reach the start_tick executor-return boundary"
        assert executions == [], "model work ran after public /new won the start_tick executor-return gap"
        assert loops.load_loop(entry.session_id).status == "cleared"
        assert runner.session_store.peek_session_id(entry.session_key) != entry.session_id
    finally:
        runner.session_store.close_all_db_handles()


# Every public LoopManager method is either a read / tick-lifecycle call or a user/agent
# control. Controls are enumerated from the class, so a new control API without fence
# coverage fails here instead of reaching review.
_LOOP_NON_CONTROL_API = frozenset({
    "state", "refresh", "is_active", "has_loop", "status_line", "is_due",
    "fire_tick", "complete_tick", "abandon_tick", "start_tick", "recover_tick",
})
_REPLACE_REQUEST = "please replace the loop with replacement work now"


def _public_loop_controls():
    return sorted(
        name for name in vars(loops.LoopManager)
        if not name.startswith("_") and name not in _LOOP_NON_CONTROL_API
    )


def _route(source):
    return {"platform": "telegram", "chat_id": source.chat_id, "chat_type": "dm", "user_id": source.user_id}


def _accepted(result):
    assert result["ok"], result
    return result


async def _session_reset(runner, source, sid):
    runner._session_db = None
    runner._cleanup_old_agent_for_reset = AsyncMock()
    runner._reset_notice_session_info = lambda source: ""
    runner._telegram_topic_new_header = lambda source: ""
    await runner._handle_reset_command(MessageEvent(text="/new", source=source))


def _pause_then_resume_elsewhere(sid):
    loops.LoopManager(sid).pause()
    loops.LoopManager(sid).resume()


# name -> (runner, source, sid) -> None | awaitable. Each control runs through a FRESH manager,
# i.e. a second manager than the one that loaded or claimed the tick.
LOOP_CONTROLS = {
    "set": lambda runner, source, sid: loops.LoopManager(sid).set(
        "replacement work", interval_seconds=60, times=2, route=_route(source)),
    "replace": lambda runner, source, sid: _accepted(loops.LoopManager(sid).replace(
        prompt="replacement work", interval_seconds=60, times=2, reason="user asked to replace it",
        user_quote="replace the loop with replacement work", user_messages=[_REPLACE_REQUEST])),
    "revise": lambda runner, source, sid: _accepted(loops.LoopManager(sid).revise(
        reason="slow the cadence", interval_seconds=120)),
    "pause": lambda runner, source, sid: loops.LoopManager(sid).pause(),
    "resume": lambda runner, source, sid: loops.LoopManager(sid).resume(),
    "clear": lambda runner, source, sid: loops.LoopManager(sid).clear(),
}
# Control paths outside the class's public methods.
LOOP_EXTRA_CONTROLS = {
    "clear_loop": lambda runner, source, sid: loops.clear_loop(sid),
    "pause_resume_second_manager": lambda runner, source, sid: _pause_then_resume_elsewhere(sid),
    "session_reset": _session_reset,
}
LOOP_CONTROL_CASES = _public_loop_controls() + sorted(LOOP_EXTRA_CONTROLS)


async def _apply_loop_control(name, runner, source, sid):
    control = LOOP_CONTROLS.get(name) or LOOP_EXTRA_CONTROLS.get(name)
    assert control is not None, f"LoopManager.{name} is a public control with no fence coverage"
    result = control(runner, source, sid)
    if asyncio.iscoroutine(result):
        await result


def test_every_public_loop_control_has_fence_coverage():
    assert set(_public_loop_controls()) == set(LOOP_CONTROLS)


def _owns_executing_tick(state, tick):
    return (
        state is not None and state.status == "active" and state.awaiting_response
        and state.claim_phase == "executing" and (state.created_at, state.ticks_fired) == tick
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("control", LOOP_CONTROL_CASES)
async def test_model_loop_fence_rechecks_every_control_after_exact_tick(control_home, control):
    """A control landing in the exact-executing-tick executor return gap either keeps the
    durable tick (and the work runs) or ends it (and no superseded work starts)."""
    runner, adapter, source, entry = native_runner(control_home)
    create_loop(entry, source)
    executions = []
    runner._hmwa_prepare_turn = AsyncMock(return_value=(
        runner._PreparedTurn([], "", "old work", "old work", None, None), {},
    ))

    async def model(**kwargs):
        executions.append(kwargs["session_id"])
        raise asyncio.CancelledError

    runner._run_agent = model
    original_executor = runner._run_in_executor_with_context
    tick = None

    async def executor(func, *args, **kwargs):
        nonlocal tick
        result = await original_executor(func, *args, **kwargs)
        if getattr(func, "__name__", "") == "_exact_executing_tick" and tick is None:
            stored = loops.load_loop(entry.session_id)
            tick = (stored.created_at, stored.ticks_fired)
            await _apply_loop_control(control, runner, source, entry.session_id)
        return result

    runner._run_in_executor_with_context = executor
    generation = runner._begin_session_run_generation(entry.session_key)
    adapter.set_message_handler(
        lambda event: runner._handle_message_with_agent(event, source, entry.session_key, generation)
    )
    try:
        await scan(runner, entry.session_id)
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        assert tick is not None, "test did not reach the exact execution return boundary"
        saved = loops.load_loop(entry.session_id)
        owned = _owns_executing_tick(saved, tick)
        if owned:
            assert executions == [entry.session_id], f"{control} kept the tick but its work never started"
        else:
            assert executions == [], f"superseded work ran after {control} won the exact execution gap"
        if control in {"set", "replace"}:
            assert not owned and saved.prompt == "replacement work"
        if control == "revise":
            assert owned, "a revise keeps the in-flight tick by design"
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.parametrize("control", LOOP_CONTROL_CASES)
def test_stale_manager_claim_never_regresses_stored_control_generation(control_home, control, monkeypatch):
    """A manager loaded before another manager's control must not write its cached control
    generation back: the stored row's generation wins, and a claim it does make passes the
    execution fence (no fenced-out claim later parked as an unknown execution)."""
    runner, _adapter, source, entry = native_runner(control_home)
    sid = entry.session_id
    try:
        create_loop(entry, source)
        stale = loops.LoopManager(sid)
        assert stale.is_due()
        asyncio.run(_apply_loop_control(control, runner, source, sid))
        controlled = loops.load_loop(sid)
        later = time.time() + 600
        monkeypatch.setattr(loops.time, "time", lambda: later)
        claimed = bool(stale.fire_tick())
        stored = loops.load_loop(sid)
        assert stored.control_generation >= controlled.control_generation, (
            f"stale claim after {control} wrote generation {stored.control_generation} "
            f"over stored {controlled.control_generation}"
        )
        assert loops.loop_control_generation(sid) == stored.control_generation
        if claimed:
            assert stored.status == "active" and stored.awaiting_response
            assert stored.created_at == controlled.created_at
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_command_loop_fence_rechecks_reset_after_command_resolution(control_home):
    runner, _adapter, source, entry = native_runner(control_home)
    mgr = loops.LoopManager(entry.session_id)
    mgr.set("/model", interval_seconds=60, times=2, route={
        "platform": "telegram", "chat_id": source.chat_id,
        "chat_type": "dm", "user_id": source.user_id,
    })
    assert mgr.fire_tick()
    saved = loops.load_loop(entry.session_id)
    event = runner._synthetic_prompt_event(source, "/model", internal=True, reply_expected=False)
    event.metadata.update({
        "hermes_loop_wakeup": True,
        "gateway_session_id": entry.session_id,
        "gateway_loop_tick": [saved.created_at, saved.ticks_fired],
    })
    effects = []
    reset = MessageEvent(text="/new", source=source)
    reset_done = False

    async def model_command(_event):
        effects.append("model")
        return "model ran"

    async def emit_collect(name, _context):
        nonlocal reset_done
        if name == "command:model" and not reset_done:
            reset_done = True
            await runner._handle_reset_command(reset)
        return []

    runner._handle_model_command = model_command
    runner.hooks.emit_collect = emit_collect
    runner._session_db = None
    runner._cleanup_old_agent_for_reset = AsyncMock()
    runner._reset_notice_session_info = lambda source: ""
    runner._telegram_topic_new_header = lambda source: ""
    try:
        handled, result = await runner._hm_dispatch_idle_commands(event, source, entry.session_key)
        assert reset_done, "test did not reach the command hook reset boundary"
        assert (handled, result) == (True, None)
        assert effects == [], "old loop command ran after public /new during command resolution"
        assert loops.load_loop(entry.session_id).status == "cleared"
        assert runner.session_store.peek_session_id(entry.session_key) != entry.session_id
    finally:
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


def loop_command_dispatch(runner, source, entry):
    async def dispatch(event):
        handled, result = await runner._hm_dispatch_idle_commands(event, source, entry.session_key)
        if handled:
            return result
        generation = runner._begin_session_run_generation(entry.session_key)
        try:
            return await runner._handle_message_with_agent(event, source, entry.session_key, generation)
        finally:
            await runner._run_post_turn_hooks(
                agent_result="LOOP_COMPLETE", source=source, is_internal=True, event=event,
            )
    return dispatch


@pytest.mark.asyncio
async def test_interrupted_effectful_loop_command_is_parked_not_refunded(control_home):
    runner, adapter, source, entry = native_runner(control_home)
    runner.config.quick_commands = {"effect": {"type": "exec", "command": "echo effect"}}
    mgr = loops.LoopManager(entry.session_id)
    mgr.set("/effect", interval_seconds=60, times=3, route={
        "platform": "telegram", "chat_id": source.chat_id,
        "chat_type": "dm", "user_id": source.user_id,
    })
    effects = []

    async def effect(command, exec_cmd):
        effects.append(loops.load_loop(entry.session_id).claim_phase)
        raise asyncio.CancelledError  # process interrupted after the external effect

    runner._hm_run_exec_quick_command = effect
    adapter.set_message_handler(loop_command_dispatch(runner, source, entry))
    try:
        await scan(runner, entry.session_id)
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        assert effects == ["executing"], "effectful command ran while its tick was still refundable"
        await scan(runner, entry.session_id)  # ownerless recovery after the interruption
        saved = loops.load_loop(entry.session_id)
        assert saved.status == "paused" and "outcome unknown" in saved.paused_reason
        assert saved.ticks_fired == 1 and not saved.awaiting_response
        await scan(runner, entry.session_id)
        assert effects == ["executing"]  # never replayed
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["owner_after_claim", "wake_already_admitted"])
async def test_loop_wake_never_answers_clarification_registered_after_claim(control_home, race):
    from tools import clarify_gateway

    runner, adapter, source, entry = native_runner(control_home)
    create_loop(entry, source)
    key = entry.session_key
    owner = asyncio.create_task(asyncio.Event().wait())
    pending = []

    def human_turn_takes_session():
        adapter._active_sessions[key] = asyncio.Event()
        adapter._session_tasks[key] = owner
        runner._running_agents[key] = object()
        pending.append(clarify_gateway.register(f"race-{race}", key, "Which option?", None))

    adapter.set_message_handler(lambda event: runner._hm_pending_reply_intercepts(event, source, key))
    try:
        if race == "owner_after_claim":
            async def worker(func, *args, **kwargs):
                result = await asyncio.to_thread(func, *args, **kwargs)
                if getattr(func, "__name__", "") == "fire_tick" and result:
                    human_turn_takes_session()  # the executor hop lets a human turn win
                return result

            runner._run_in_executor_with_context = worker
            await scan(runner, entry.session_id)
            saved = loops.load_loop(entry.session_id)
            assert not saved.awaiting_response and saved.ticks_fired == 0  # unstarted claim refunded
        else:
            # Structural guard: even an already-built gateway wake cannot answer a human prompt.
            human_turn_takes_session()
            wake = runner._synthetic_prompt_event(source, "loop wake text", internal=True, reply_expected=False)
            wake.metadata.update({"hermes_loop_wakeup": True})
            await adapter.handle_message(wake)
            assert await runner._hm_pending_reply_intercepts(wake, source, key) is None
        assert pending and not pending[0].event.is_set(), "a synthetic loop wake answered the human clarification"
        assert adapter._session_tasks[key] is owner and not owner.done()
        await adapter.handle_message(MessageEvent(text="Use the blue option", source=source))
        assert clarify_gateway.wait_for_response(pending[0].clarify_id, 1) == "Use the blue option"
    finally:
        for entry_ in pending:
            clarify_gateway.resolve_gateway_clarify(entry_.clarify_id, "cleanup")
            clarify_gateway.wait_for_response(entry_.clarify_id, 1)
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["pause", "clear", "replace", "session_reset"])
async def test_command_fence_survives_profile_hydration(control_home, monkeypatch, control):
    """A control landing while the native profile secret loader hydrates must win."""
    runner, _adapter, source, entry = native_runner(control_home)
    runner.config.multiplex_profiles = True
    runner._resolve_profile_home_for_source = lambda _source: control_home
    mgr = loops.LoopManager(entry.session_id)
    mgr.set("/verbose", interval_seconds=60, times=2, route={
        "platform": "telegram", "chat_id": source.chat_id,
        "chat_type": "dm", "user_id": source.user_id,
    })
    fired = mgr.fire_tick()
    assert fired == "/verbose"
    state = mgr.state
    tick = (state.created_at, state.ticks_fired)
    hydration_started = threading.Event()
    release_hydration = threading.Event()
    effects = []

    def blocked_secret_loader(_profile_home):
        # A concurrent /new may enter its own profile scope; block only this dispatch's
        # first hydration worker, not the control that must be allowed to supersede it.
        if hydration_started.is_set():
            return {}
        hydration_started.set()
        assert release_hydration.wait(5)
        return {}

    from gateway import run as gateway_run
    monkeypatch.setattr(gateway_run, "_load_profile_secret_scope", blocked_secret_loader)
    runner._handle_verbose_command = AsyncMock(side_effect=lambda _event: effects.append("handler"))
    event = MessageEvent(
        text="/verbose", source=source, internal=True,
        metadata={
            "hermes_loop_wakeup": True,
            "gateway_loop_tick": list(tick),
            "gateway_session_id": entry.session_id,
        },
    )
    dispatch = asyncio.create_task(runner._hm_dispatch_idle_commands(event, source, entry.session_key))
    assert await asyncio.to_thread(hydration_started.wait, 5), "native hydration was not reached"
    await _apply_loop_control(control, runner, source, entry.session_id)
    release_hydration.set()
    result = await dispatch
    assert result == (True, None)
    assert effects == []
    assert not _owns_executing_tick(loops.load_loop(entry.session_id), tick)
    runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_command_final_fence_and_handler_share_event_loop_step(control_home):
    runner, _adapter, source, entry = native_runner(control_home)
    mgr = loops.LoopManager(entry.session_id)
    mgr.set("/verbose", interval_seconds=60, times=2, route={
        "platform": "telegram", "chat_id": source.chat_id,
        "chat_type": "dm", "user_id": source.user_id,
    })
    assert mgr.fire_tick() == "/verbose"
    state = mgr.state
    tick = (state.created_at, state.ticks_fired)
    steps = []
    task = asyncio.current_task()
    original_fence = runner._loop_wake_execution_fence_sync

    def fenced(event, fence):
        steps.append(("fence", asyncio.current_task()))
        asyncio.get_running_loop().call_soon(lambda: steps.append(("yielded", None)))
        return original_fence(event, fence)

    async def handler(_event):
        assert steps[-1][0] == "fence", "effect entry yielded after the final fence"
        assert steps[-1][1] is task
        steps.append(("handler", asyncio.current_task()))
        return "ok"

    runner._loop_wake_execution_fence_sync = fenced
    runner._handle_verbose_command = handler
    event = MessageEvent(
        text="/verbose", source=source, internal=True,
        metadata={
            "hermes_loop_wakeup": True,
            "gateway_loop_tick": list(tick),
            "gateway_session_id": entry.session_id,
        },
    )
    handled, result = await runner._hm_dispatch_idle_commands(event, source, entry.session_key)
    assert (handled, result) == (True, "ok")
    assert [step[0] for step in steps[:2]] == ["fence", "handler"]
    assert steps[0][1] is steps[1][1] is task
    runner.session_store.close_all_db_handles()


def test_shared_loop_effect_entry_has_no_preparation_after_final_fence():
    """Guard the structure, not a particular preparation boundary in a caller."""
    import ast
    import inspect
    import textwrap

    for name in ("_invoke_loop_wake_effect", "_invoke_loop_wake_sync_effect"):
        function = getattr(GatewayRunner, name)
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        body = tree.body[0].body
        assert isinstance(body[-2], ast.If)
        assert isinstance(body[-2].test, ast.UnaryOp)
        assert body[-2].test.operand.func.attr == "_loop_wake_execution_fence_sync"
        invocation = body[-1]
        assert isinstance(invocation, ast.Return)
        awaits = [node for node in ast.walk(invocation) if isinstance(node, ast.Await)]
        if inspect.iscoroutinefunction(function):
            assert len(awaits) == 1
            assert isinstance(awaits[0].value, ast.Call)
            assert awaits[0].value.func.id == "handler"
        else:
            assert not awaits
