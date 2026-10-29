"""Native persisted loop claims, real adapter admission and explicit conversation reset.

Only the model/transport are controlled. SessionStore, metadata stores, public
/new handler, turn entry and the gateway's existing watcher are exercised.
"""
import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import hermes_loop_guard
import pytest

from evals.heartbeat_idle_wire import WireAdapter
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from gateway.turn_lease import SessionTurnLeaseRegistry
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
    runner._turn_leases = SessionTurnLeaseRegistry()
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
    state = await asyncio.to_thread(loops.load_loop, sid)
    await runner._loop_wakeup_fire_one(sid, state, time.time() + 86400, set())


@pytest.mark.asyncio
async def test_refused_adapter_admission_consumes_tick_until_recovery(control_home):
    runner, adapter, source, entry = native_runner(control_home)
    create_loop(entry, source)
    # Real BasePlatformAdapter: with no message handler admission is refused.
    await scan(runner, entry.session_id)
    saved = loops.load_loop(entry.session_id)
    assert saved.awaiting_response and saved.claim_phase == "started"
    assert saved.ticks_fired == 1
    await scan(runner, entry.session_id)
    saved = loops.load_loop(entry.session_id)
    assert not saved.awaiting_response and saved.status == "active"
    assert saved.ticks_fired == 1
    runner.session_store.close_all_db_handles()


def test_original_claim_stays_not_due_until_reconciled(control_home):
    _runner, _adapter, source, entry = native_runner(control_home)
    mgr = create_loop(entry, source)
    assert mgr.fire_tick()
    later = loops.LoopManager(entry.session_id)
    assert later.state.awaiting_response is True
    assert later.is_due(time.time() + 86400) is False


@pytest.mark.asyncio
async def test_legacy_claim_with_unknown_effect_is_skipped_not_replayed(control_home):
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
    assert parked.status == "active"
    assert parked.awaiting_response is False
    assert parked.ticks_fired == 1
    assert not adapter._session_tasks
    runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_restart_skips_started_tick_without_replaying_it(control_home):
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
    assert saved.ticks_fired == 1
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
@pytest.mark.parametrize("prompt", ["/status", "check the queue"])
async def test_due_loop_defers_while_human_turn_owns_generation_and_lease(control_home, prompt):
    """A due command or model wake cannot invalidate or consume a live human turn."""
    runner, adapter, source, entry = native_runner(control_home)
    manager = loops.LoopManager(entry.session_id)
    manager.set(prompt, interval_seconds=60, times=2, route={
        "platform": "telegram", "chat_id": source.chat_id,
        "chat_type": "dm", "user_id": source.user_id,
    })
    manager.state.next_due_at = 0
    manager._save()

    delivered = []

    async def handle(event):
        delivered.append(event.text)
        event._gateway_accepted = True
        if event.text.lstrip().startswith("/"):
            event._gateway_inline_command_handled = True
        return "human result"

    adapter.set_message_handler(handle)
    key = entry.session_key
    generation = runner._begin_session_run_generation(key)
    lease = await runner._turn_leases.acquire(
        entry.session_id, owner_key=key, generation=generation, timeout=1.0,
    )
    state = runner._session_state(key)
    state.turn.lease_tokens[generation] = lease
    runner._running_agents[key] = object()

    await scan(runner, entry.session_id)
    saved = loops.load_loop(entry.session_id)
    assert runner._current_session_run_generation(key) == generation
    assert saved.ticks_fired == 0 and not saved.awaiting_response
    assert delivered == []

    runner._release_turn_lease(key, generation)
    assert generation not in state.turn.lease_tokens
    assert runner._release_running_agent_state(key, run_generation=generation)
    assert key not in runner._running_agents

    await scan(runner, entry.session_id)
    await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
    saved = loops.load_loop(entry.session_id)
    assert saved.ticks_fired == 1
    assert len(delivered) == 1

    # A second scan cannot replay the started tick, even while the synthetic turn is still settling.
    await scan(runner, entry.session_id)
    assert loops.load_loop(entry.session_id).ticks_fired == 1
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
@pytest.mark.parametrize("phase", ["started", "claimed", "executing", "legacy"])
@pytest.mark.parametrize("response", ["Answer to an unrelated user question", "LOOP_COMPLETE", ""])
async def test_post_turn_requires_exact_loop_wake_identity(control_home, phase, response):
    runner, adapter, source, entry = native_runner(control_home)
    try:
        mgr = create_loop(entry, source)
        assert mgr.fire_tick()
        state = mgr.state
        assert state is not None
        tick = (state.created_at, state.ticks_fired)
        state.claim_phase = "" if phase == "legacy" else phase
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
@pytest.mark.parametrize("observer", [False, True])
async def test_native_loop_command_settles_only_after_its_dispatch_path(control_home, prompt, observer):
    from gateway.hooks import HookRegistry

    runner, adapter, source, entry = native_runner(control_home)
    runner.hooks = HookRegistry()  # exercise an actual empty registry, not a missing dispatcher
    observer_runs = []
    if observer:
        async def observe(_name, _context):
            saved = await asyncio.to_thread(loops.load_loop, entry.session_id)
            observer_runs.append((saved.claim_phase, saved.run_generation))
        runner.hooks._handlers["command:*"] = [observe]
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
        fence = event._gateway_loop_execution_fence
        if not runner._loop_wake_execution_fence_sync(event, fence):
            return None
        generation = fence[2]
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
        assert executions == ([] if prompt == "/start" else [(True, "started", 1)])
        assert observer_runs == ([("started", 1)] if observer else [])
        if prompt != "/start":
            assert runner._current_session_run_generation(entry.session_key) == 1
            runner._send_command_ack.assert_not_awaited()  # synthetic prompt builds have no ack effect
        saved = loops.load_loop(entry.session_id)
        assert not saved.awaiting_response and saved.ticks_fired == 1
        assert saved.status == "active"  # cancellation/silent command schedules the next tick
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", ["plan", "learn"])
@pytest.mark.parametrize("loop_wake", [False, True])
async def test_hook_rewritten_prompt_command_uses_canonical_builder(control_home, canonical, loop_wake):
    from agent.learn_prompt import build_learn_prompt
    from agent.plan_prompt import build_plan_prompt
    from gateway.hooks import HookRegistry

    runner, _adapter, source, entry = native_runner(control_home)
    hooks = HookRegistry()
    runner.hooks = hooks
    runner._send_command_ack = AsyncMock()
    request = "inspect my work"
    hook_runs = []

    async def rewrite(_name, _context):
        hook_runs.append(_context["command"])
        return {"decision": "rewrite", "command_name": canonical, "raw_args": request}

    hooks._handlers["command:help"] = [rewrite]
    direct = MessageEvent(text=f"/{canonical} {request}", source=source)
    rewritten = MessageEvent(text="/help", source=source, internal=loop_wake)
    expected = {"plan": build_plan_prompt, "learn": build_learn_prompt}[canonical](request)
    try:
        assert await runner._hm_dispatch_idle_commands(direct, source, entry.session_key) == (False, None)
        assert direct.text == expected
        runner._send_command_ack.reset_mock()
        if loop_wake:
            mgr = loops.LoopManager(entry.session_id)
            mgr.set("/help", interval_seconds=60, times=2, route=_route(source))
            assert mgr.fire_tick() == "/help"
            assert mgr.state is not None
            tick = (mgr.state.created_at, mgr.state.ticks_fired)
            rewritten.metadata.update({
                "hermes_loop_wakeup": True, "gateway_session_id": entry.session_id,
                "gateway_loop_tick": list(tick),
            })
        hermes_loop_guard.set_loop_thread(threading.get_ident())
        try:
            result = await runner._hm_dispatch_idle_commands(rewritten, source, entry.session_key)
        finally:
            hermes_loop_guard.set_loop_thread(None)
        assert result == (False, None)
        assert hook_runs == ["help"]
        assert rewritten.text == direct.text == expected
        if loop_wake:
            runner._send_command_ack.assert_not_awaited()
            saved = loops.load_loop(entry.session_id)
            assert saved is not None
            assert saved.awaiting_response and saved.claim_phase == "started"
            assert rewritten._gateway_loop_execution_fence[2] == 0
            assert runner._loop_wake_execution_fence_sync(rewritten, rewritten._gateway_loop_execution_fence)
        else:
            runner._send_command_ack.assert_awaited_once()
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["_exact_started_tick", "_check_loop_execution"])
async def test_command_loop_fence_rechecks_reset_after_admission_check(control_home, boundary):
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
@pytest.mark.parametrize("boundary", ["_exact_started_tick", "_check_loop_execution"])
async def test_model_loop_fence_rechecks_reset_after_admission_check(control_home, boundary):
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
        assert reset_done, "test did not reach the admission-check executor-return boundary"
        assert executions == [], "model work ran after public /new won the admission-check executor-return gap"
        assert loops.load_loop(entry.session_id).status == "cleared"
        assert runner.session_store.peek_session_id(entry.session_key) != entry.session_id
    finally:
        runner.session_store.close_all_db_handles()


# Every public LoopManager method is either a read / tick-lifecycle call or a user/agent
# control. Controls are enumerated from the class, so a new control API without fence
# coverage fails here instead of reaching review.
_LOOP_NON_CONTROL_API = frozenset({
    "state", "refresh", "is_active", "has_loop", "status_line", "is_due",
    "fire_tick", "complete_tick", "abandon_tick", "recover_tick",
})
_REPLACE_REQUEST = "please replace the loop with replacement work now"


def _public_loop_controls():
    # Inherited controls count too: revise and replace live on the revisions mixin.
    return sorted({
        name for cls in loops.LoopManager.__mro__[:-1] for name in vars(cls)
        if not name.startswith("_") and name not in _LOOP_NON_CONTROL_API
    })


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
    "wake_now_for_external_event": lambda runner, source, sid: loops.LoopManager(sid).wake_now_for_external_event(),
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


def _owns_started_tick(state, tick):
    return (
        state is not None and state.status == "active" and state.awaiting_response
        and state.claim_phase == "started" and (state.created_at, state.ticks_fired) == tick
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("control", LOOP_CONTROL_CASES)
async def test_model_loop_fence_rechecks_every_control_after_exact_tick(control_home, control):
    """A control landing in the exact-started-tick executor return gap either keeps the
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
        if getattr(func, "__name__", "") == "_exact_started_tick" and tick is None:
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
        owned = _owns_started_tick(saved, tick)
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
    execution fence (no fenced-out claim later consumed as an unknown execution)."""
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
        # A retired loop leaves no local fence, so its stale wakes fail the sync check closed.
        expected = None if stored.status == "cleared" else stored.control_generation
        assert loops.loop_control_generation(sid, default=None) == expected
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
    create_loop(entry, source)
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
        fence = event._gateway_loop_execution_fence
        if not runner._loop_wake_execution_fence_sync(event, fence):
            return None
        generation = fence[2]
        try:
            return await runner._handle_message_with_agent(event, source, entry.session_key, generation)
        finally:
            await runner._run_post_turn_hooks(
                agent_result="LOOP_COMPLETE", source=source, is_internal=True, event=event,
            )
    return dispatch


@pytest.mark.asyncio
async def test_interrupted_effectful_loop_command_is_skipped_not_refunded(control_home):
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
        assert effects == ["started"], "effectful command entered without a durably started admission"
        await scan(runner, entry.session_id)  # ownerless recovery after the interruption
        saved = loops.load_loop(entry.session_id)
        assert saved.status == "active"
        assert saved.ticks_fired == 1 and not saved.awaiting_response
        assert saved.next_due_at > time.time()
        assert effects == ["started"]  # recovery did not replay tick 1
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["owner_before_claim", "owner_after_claim", "wake_already_admitted"])
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
        if race in {"owner_before_claim", "owner_after_claim"}:
            async def worker(func, *args, **kwargs):
                claiming = getattr(getattr(func, "func", func), "__name__", "") == "fire_tick"
                if claiming and race == "owner_before_claim":
                    human_turn_takes_session()
                result = await asyncio.to_thread(func, *args, **kwargs)
                if claiming and result and race == "owner_after_claim":
                    human_turn_takes_session()  # the executor hop lets a human turn win
                return result

            runner._run_in_executor_with_context = worker
            await scan(runner, entry.session_id)
            saved = loops.load_loop(entry.session_id)
            if race == "owner_after_claim":
                assert saved.awaiting_response and saved.ticks_fired == 1  # admission is never refunded
            else:
                assert not saved.awaiting_response and saved.ticks_fired == 0  # failed CAS ran no work
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
    """A legacy-stamped wake without a session key still installs a fence before hydration."""
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
    assert event._gateway_loop_execution_fence[:2] == (entry.session_id, tick)
    assert effects == []
    assert not _owns_started_tick(loops.load_loop(entry.session_id), tick)
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
    handler_index = next(i for i, step in enumerate(steps) if step[0] == "handler")
    assert steps[handler_index - 1][0] == "fence"
    assert steps[handler_index - 1][1] is steps[handler_index][1] is task
    runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_native_loop_command_hook_supersede_after_stored_fence_check(control_home):
    from gateway.hooks import HookRegistry

    runner, adapter, source, entry = native_runner(control_home)
    loops.LoopManager(entry.session_id).set(
        "/learn reusable checking", interval_seconds=60, times=2, route=_route(source),
    )
    runner.hooks = HookRegistry()
    hook_effect = AsyncMock()
    runner.hooks._handlers["command:*"] = [hook_effect]
    runner._run_agent = AsyncMock()
    runner._send_command_ack = AsyncMock()
    original_executor = runner._run_in_executor_with_context
    superseded = False

    async def executor(func, *args, **kwargs):
        nonlocal superseded
        result = await original_executor(func, *args, **kwargs)
        if getattr(func, "__name__", "") == "_check_loop_execution" and not superseded:
            assert result is True
            started = await asyncio.to_thread(loops.load_loop, entry.session_id)
            assert started.claim_phase == "started"
            superseded = True
            await _session_reset(runner, source, entry.session_id)
        return result

    runner._run_in_executor_with_context = executor
    adapter.set_message_handler(loop_command_dispatch(runner, source, entry))
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    try:
        await scan(runner, entry.session_id)
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
    finally:
        hermes_loop_guard.set_loop_thread(None)
    try:
        assert superseded, "test did not reach the stored fence check return gap"
        hook_effect.assert_not_awaited()
        runner._run_agent.assert_not_awaited()
        assert loops.load_loop(entry.session_id).status == "cleared"
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_real_admission_superseded_command_cannot_release_successor(control_home):
    """A successor admitted during the command-check executor gap keeps its run state and lease."""
    runner, adapter, source, entry = native_runner(control_home)
    loops.LoopManager(entry.session_id).set(
        "/learn reusable checking", interval_seconds=60, times=2, route=_route(source),
    )
    runner._send_command_ack = AsyncMock()
    runner._run_agent = AsyncMock()
    original_executor = runner._run_in_executor_with_context
    successor = object()
    installed = False
    successor_generation = None

    async def executor(func, *args, **kwargs):
        nonlocal installed, successor_generation
        result = await original_executor(func, *args, **kwargs)
        if getattr(func, "__name__", "") == "_check_loop_execution" and not installed:
            installed = True
            old_generation = runner._current_session_run_generation(entry.session_key)
            runner._release_running_agent_state(entry.session_key, run_generation=old_generation)
            successor_generation = runner._invalidate_session_run_generation(
                entry.session_key, reason="test successor",
            )
            state = runner._session_state(entry.session_key)
            state.turn.agent = successor
            token = await runner._turn_leases.acquire(
                entry.session_id, owner_key=entry.session_key,
                generation=successor_generation, timeout=1.0,
            )
            state.turn.lease_tokens[successor_generation] = token
        return result

    runner._run_in_executor_with_context = executor
    adapter.set_message_handler(runner._handle_admitted_message)
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    try:
        await scan(runner, entry.session_id)
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
    finally:
        hermes_loop_guard.set_loop_thread(None)
    try:
        assert installed, "test did not reach the real command-check executor boundary"
        assert successor_generation == 2
        runner._run_agent.assert_not_awaited()
        state = runner._session_state(entry.session_key)
        assert state.turn.agent is successor
        assert successor_generation in state.turn.lease_tokens
        # No command effect ran: preserve the admitted claim for later skip-only recovery.
        saved = loops.load_loop(entry.session_id)
        assert saved is not None
        assert saved.status == "active" and saved.awaiting_response and saved.ticks_fired == 1
        assert saved.claim_phase == "started"
        assert adapter.wire == []
    finally:
        runner._release_turn_lease(entry.session_key, successor_generation)
        runner._release_running_agent_state(entry.session_key, run_generation=successor_generation)
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["/learn reusable checking", "/plan disposable work"])
async def test_native_loop_command_reset_between_fallthrough_and_model_entry(control_home, prompt):
    runner, adapter, source, entry = native_runner(control_home)
    mgr = loops.LoopManager(entry.session_id)
    mgr.set(prompt, interval_seconds=60, times=2, route=_route(source))
    runner._send_command_ack = AsyncMock()
    runner._run_agent = AsyncMock()
    original_dispatch = runner._hm_dispatch_idle_commands
    fallthroughs = []

    async def dispatch_then_reset(event, source, key):
        handled, result = await original_dispatch(event, source, key)
        if not handled:
            fallthroughs.append(event._gateway_loop_execution_fence)
            saved = await asyncio.to_thread(loops.load_loop, entry.session_id)
            assert saved.claim_phase == "started" and saved.run_generation == 1
            await _session_reset(runner, source, entry.session_id)  # the real public /new path
        return handled, result

    runner._hm_dispatch_idle_commands = dispatch_then_reset
    adapter.set_message_handler(loop_command_dispatch(runner, source, entry))
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    try:
        await scan(runner, entry.session_id)
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
    finally:
        hermes_loop_guard.set_loop_thread(None)
    try:
        assert len(fallthroughs) == 1, "test did not reach the command-to-model fallthrough"
        runner._run_agent.assert_not_awaited()
        # /new bumps G to G+1; the stale wake must not bump it AGAIN and resurrect its ownership.
        assert runner._current_session_run_generation(entry.session_key) == 2
        assert runner.session_store.peek_session_id(entry.session_key) != entry.session_id
        assert loops.load_loop(entry.session_id).status == "cleared"
    finally:
        runner.session_store.close_all_db_handles()


def test_loop_tick_admission_token_is_exact_including_zero(control_home):
    runner, _adapter, source, entry = native_runner(control_home)
    mgr = create_loop(entry, source)
    assert mgr.fire_tick(0)
    tick = (mgr.state.created_at, mgr.state.ticks_fired)
    control = mgr.state.control_generation
    try:
        assert mgr.state.claim_phase == "started"
        assert mgr._tick_execution_current(tick, control_generation=control, run_generation=0)
        for generation in (1, 2):
            assert not mgr._tick_execution_current(
                tick, control_generation=control, run_generation=generation,
            )
        assert loops.load_loop(entry.session_id).run_generation == 0
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", ["model", "quick_command", "command_hook"])
@pytest.mark.parametrize("supersede", ["pause_resume", "generation"])
async def test_loop_effect_cas_rejects_cross_process_control_without_local_cache_update(
    control_home, producer, supersede,
):
    import os
    import subprocess
    import sys
    from gateway.hooks import HookRegistry

    runner, adapter, source, entry = native_runner(control_home)
    runner._hmwa_prepare_turn = AsyncMock(return_value=(
        runner._PreparedTurn([], "", "old work", "old work", None, None), {},
    ))
    effects = []

    async def effect(*args, **kwargs):
        effects.append(producer)
        return "LOOP_COMPLETE"

    if producer == "model":
        create_loop(entry, source)
        runner._run_agent = effect
        generation = runner._begin_session_run_generation(entry.session_key)
        adapter.set_message_handler(
            lambda event: runner._handle_message_with_agent(event, source, entry.session_key, generation)
        )
    else:
        runner.hooks = HookRegistry()
        if producer == "quick_command":
            prompt = "/effect"
            runner.config.quick_commands = {"effect": {"type": "exec", "command": "echo effect"}}
            runner._hm_run_exec_quick_command = effect
        else:
            prompt = "/start"
            runner.hooks._handlers["command:*"] = [effect]
        loops.LoopManager(entry.session_id).set(prompt, interval_seconds=60, times=2, route=_route(source))
        adapter.set_message_handler(loop_command_dispatch(runner, source, entry))

    original_executor = runner._run_in_executor_with_context
    controlled = False

    async def executor(func, *args, **kwargs):
        nonlocal controlled
        result = await original_executor(func, *args, **kwargs)
        if getattr(func, "__name__", "") == "_exact_started_tick" and not controlled:
            controlled = True
            local_control = loops.loop_control_generation(entry.session_id)
            if supersede == "pause_resume":
                code = (
                    "import sys; from hermes_cli.loops import LoopManager; "
                    "LoopManager(sys.argv[1]).pause(); LoopManager(sys.argv[1]).resume()"
                )
            else:
                code = (
                    "import sys; from hermes_cli.loops import LoopManager, save_loop; "
                    "mgr = LoopManager(sys.argv[1]); mgr.state.run_generation += 1; "
                    "save_loop(sys.argv[1], mgr.state, tick_lifecycle=True)"
                )
            child = await asyncio.to_thread(
                subprocess.run, [sys.executable, "-c", code, entry.session_id],
                env={**os.environ, "HERMES_HOME": str(control_home)},
                capture_output=True, text=True, timeout=60,
            )
            assert child.returncode == 0, child.stderr
            assert loops.loop_control_generation(entry.session_id) == local_control
        return result

    runner._run_in_executor_with_context = executor
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    try:
        await scan(runner, entry.session_id)
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
    finally:
        hermes_loop_guard.set_loop_thread(None)
    try:
        assert controlled, "test did not land the separate-process control before effect CAS"
        assert effects == [], "the durable effect fence trusted a stale local cache"
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["admission", "preparation", "agent_start", "model"])
async def test_atomic_admission_interruption_skips_tick_and_next_tick_runs(control_home, interruption):
    """An admitted wake is consumed even if a producer performs effects then cancels."""
    runner, adapter, source, entry = native_runner(control_home)
    create_loop(entry, source)
    effects = []
    models = []
    cancelled = False

    async def prepare(*args):
        nonlocal cancelled
        if interruption == "preparation" and not cancelled:
            effects.append("preparation")
            cancelled = True
            raise asyncio.CancelledError
        return runner._PreparedTurn([], "", "work", "work", None, None), {}

    async def emit(name, context):
        nonlocal cancelled
        if name == "agent:start" and interruption == "agent_start" and not cancelled:
            effects.append("agent:start")
            cancelled = True
            raise asyncio.CancelledError

    async def model(**kwargs):
        nonlocal cancelled
        saved = await asyncio.to_thread(loops.load_loop, entry.session_id)
        models.append(saved.ticks_fired)
        if interruption == "model" and not cancelled:
            effects.append("model")
            cancelled = True
        raise asyncio.CancelledError

    runner._hmwa_prepare_turn = prepare
    runner.hooks.emit = emit
    runner._run_agent = model

    async def dispatch(event):
        nonlocal cancelled
        if interruption == "admission" and not cancelled:
            cancelled = True
            raise asyncio.CancelledError
        fence = getattr(event, "_gateway_loop_execution_fence", None)
        generation = fence[2] if fence is not None else runner._current_session_run_generation(entry.session_key)
        return await runner._handle_message_with_agent(event, source, entry.session_key, generation)

    adapter.set_message_handler(dispatch)
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    try:
        await scan(runner, entry.session_id)
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        saved = await asyncio.to_thread(loops.load_loop, entry.session_id)
        assert cancelled
        assert saved.awaiting_response and saved.claim_phase == "started"
        assert saved.ticks_fired == 1
        await scan(runner, entry.session_id)  # ownerless recovery, never replay
        saved = await asyncio.to_thread(loops.load_loop, entry.session_id)
        assert saved.status == "active" and not saved.awaiting_response
        assert saved.ticks_fired == 1
        saved.next_due_at = time.time() - 1
        await asyncio.to_thread(loops.save_loop, entry.session_id, saved)
        await scan(runner, entry.session_id)
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        assert models == ([1, 2] if interruption == "model" else [2])
        assert effects == ([] if interruption == "admission" else [{"preparation": "preparation", "agent_start": "agent:start", "model": "model"}[interruption]])
    finally:
        hermes_loop_guard.set_loop_thread(None)
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_learn_final_command_check_reset_preserves_successor_agent_and_lease(control_home):
    """Public /new in the final command-check return gap cannot lend the stale wake a token."""
    runner, adapter, source, entry = native_runner(control_home)
    loops.LoopManager(entry.session_id).set(
        "/learn reusable checking", interval_seconds=60, times=2, route=_route(source),
    )
    runner._send_command_ack = AsyncMock()
    runner._run_agent = AsyncMock()
    original_canonical = runner._hm_dispatch_canonical_command
    original_current = runner._loop_wake_execution_current
    prompt_built = False
    successor = object()
    installed = False
    generation = None

    async def canonical(event, source, key, name):
        nonlocal prompt_built
        result = await original_canonical(event, source, key, name)
        prompt_built = name == "learn" and not result[0]
        return result

    async def current(sid, source_tick, wake_source, **kwargs):
        nonlocal installed, generation
        result = await original_current(sid, source_tick, wake_source, **kwargs)
        if prompt_built and not installed:
            await _session_reset(runner, source, entry.session_id)
            generation = runner._current_session_run_generation(entry.session_key)
            if generation < 2:
                generation = runner._begin_session_run_generation(entry.session_key)
            state = runner._session_state(entry.session_key)
            state.turn.agent = successor
            successor_sid = await asyncio.to_thread(runner.session_store.peek_session_id, entry.session_key)
            token = await runner._turn_leases.acquire(
                successor_sid, owner_key=entry.session_key,
                generation=generation, timeout=1.0,
            )
            state.turn.lease_tokens[generation] = token
            installed = True
        return result

    runner._hm_dispatch_canonical_command = canonical
    runner._loop_wake_execution_current = current
    adapter.set_message_handler(runner._handle_admitted_message)
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    try:
        await scan(runner, entry.session_id)
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        assert installed
        assert generation == 2
        runner._run_agent.assert_not_awaited()
        state = runner._session_state(entry.session_key)
        assert state.turn.agent is successor
        assert generation in state.turn.lease_tokens
    finally:
        hermes_loop_guard.set_loop_thread(None)
        runner._release_turn_lease(entry.session_key, generation)
        runner._release_running_agent_state(entry.session_key, run_generation=generation)
        runner.session_store.close_all_db_handles()


async def _prepare_loop_command_dispatch(runner, source, entry, prompt):
    mgr = loops.LoopManager(entry.session_id)
    mgr.set(prompt, interval_seconds=60, times=1, route=_route(source))
    generation = runner._begin_session_run_generation(entry.session_key)
    assert mgr.fire_tick(generation)
    assert mgr.state is not None
    event = runner._synthetic_prompt_event(source, prompt, internal=True, reply_expected=False)
    event.metadata.update({
        "hermes_loop_wakeup": True,
        "gateway_session_id": entry.session_id,
        "gateway_loop_tick": [mgr.state.created_at, mgr.state.ticks_fired],
    })
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("command_kind", [
    "hook", "plain", "canonical", "exec", "plugin", "sync_plugin", "coroutine_plugin", "learn", "plan",
])
@pytest.mark.parametrize("suppress", [True, False], ids=["suppressed", "ran"])
async def test_fence_suppressed_command_dispatch_keeps_started_tick(
    control_home, monkeypatch, command_kind, suppress,
):
    """Invalidate inside the effect-entry await, not an earlier dispatch preparation check."""
    from gateway.hooks import HookRegistry

    runner, _adapter, source, entry = native_runner(control_home)
    runner.hooks = HookRegistry()
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    runner._run_agent = AsyncMock()
    # Keep the actual manager bound and execute the real stored-row settlement.
    original_complete = loops.LoopManager.complete_tick
    decisions = []

    def track_complete(manager, *args, **kwargs):
        decisions.append((args, kwargs))
        return original_complete(manager, *args, **kwargs)

    monkeypatch.setattr(loops.LoopManager, "complete_tick", track_complete)
    prompt = {
        "hook": "/start", "plain": "/help", "canonical": "/start", "exec": "/effect",
        "plugin": "/plugin-effect", "sync_plugin": "/plugin-effect", "coroutine_plugin": "/plugin-effect",
        "learn": "/learn inspect work", "plan": "/plan inspect work",
    }[command_kind]
    event = await _prepare_loop_command_dispatch(runner, source, entry, prompt)
    effect = AsyncMock(return_value="effect ran")
    sync_calls = []

    if command_kind == "hook":
        runner.hooks._handlers["command:*"] = [AsyncMock(return_value={"decision": "handle"})]
    elif command_kind == "plain":
        runner._gateway_plain_command_handlers = lambda: {"help": effect}
    elif command_kind == "canonical":
        runner._hm_cmd_start = AsyncMock(wraps=runner._hm_cmd_start)
        effect = runner._hm_cmd_start
    elif command_kind == "exec":
        runner._hm_quick_commands = lambda: {"effect": {"type": "exec", "command": "not executed"}}
        runner._hm_run_exec_quick_command = effect
    elif "plugin" in command_kind:
        def sync_plugin(_args):
            sync_calls.append(_args)
            return effect() if command_kind == "coroutine_plugin" else "effect ran"
        plugin = effect if command_kind == "plugin" else sync_plugin
        monkeypatch.setattr("hermes_cli.plugins.get_plugin_command_handler", lambda name: plugin)

    original_effect = runner._invoke_loop_wake_effect
    original_executor = runner._run_in_executor_with_context
    armed = False
    invalidated = False

    async def invoke(event, handler, *args, **kwargs):
        nonlocal armed
        is_hook = getattr(handler, "__name__", "") == "_dispatch_hooks"
        armed = (is_hook if command_kind == "hook" else not is_hook)
        return await original_effect(event, handler, *args, **kwargs)

    async def executor(func, *args, **kwargs):
        nonlocal invalidated
        name = getattr(func, "__name__", "")
        if command_kind == "sync_plugin" and name == "_invoke_loop_wake_sync_effect" and suppress:
            await asyncio.sleep(0)
            runner._begin_session_run_generation(entry.session_key)
            invalidated = True
        result = await original_executor(func, *args, **kwargs)
        if armed and name == "_check_loop_execution" and suppress and not invalidated:
            runner._begin_session_run_generation(entry.session_key)
            invalidated = True
        return result

    runner._invoke_loop_wake_effect = invoke
    runner._run_in_executor_with_context = executor
    try:
        handled, result = await runner._hm_dispatch_idle_commands(event, source, entry.session_key)
        saved = loops.load_loop(entry.session_id)
        assert saved is not None
        if suppress:
            assert invalidated, "test failed to reach the effect-entry await"
            assert handled and result is None
            assert decisions == []
            runner._defer_goal_status_notice_after_delivery.assert_not_awaited()
            runner._run_agent.assert_not_awaited()
            effect.assert_not_awaited()
            assert saved.awaiting_response and saved.claim_phase == "started"
            assert saved.status == "active" and saved.ticks_fired == 1
        elif command_kind in {"learn", "plan"}:
            assert not handled and result is None
            assert decisions == []  # rewriting is not completion: the model settles this wake
            assert event.text != prompt
            await runner._post_turn_loop_completion(
                session_entry=entry, source=source, final_response="model ran",
                loop_tick=tuple(event.metadata["gateway_loop_tick"]),
            )
            assert len(decisions) == 1
        else:
            assert handled
            assert len(decisions) == 1
            assert not saved.awaiting_response and saved.status == "done"
            runner._defer_goal_status_notice_after_delivery.assert_awaited_once()
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("migration_fails", [False, True])
async def test_matching_compression_tip_rehomes_loop_without_losing_claim(control_home, monkeypatch, migration_fails):
    runner, adapter, source, entry = native_runner(control_home)
    mgr = create_loop(entry, source)
    assert mgr.fire_tick(7)
    before = loops.load_loop(entry.session_id)
    assert before is not None
    old_sid = entry.session_id
    tip = "compressed-child"
    current = SimpleNamespace(session_id=tip)
    runner.session_store.lookup_by_session_key = lambda key: current
    runner.session_store._db_for_key = lambda key: SimpleNamespace(get_compression_tip=lambda sid: tip)
    if migration_fails:
        monkeypatch.setattr(loops, "migrate_loop_to_session", Mock(return_value=False))
    try:
        await runner._loop_wakeup_fire_one(old_sid, before, time.time() + 86400, set())
        parent = loops.load_loop(old_sid)
        assert parent is not None
        child = loops.load_loop(tip)
        if migration_fails:
            assert parent.to_json() == before.to_json()
            assert child is None
            loops.migrate_loop_to_session.assert_called_once_with(old_sid, tip, reason="compression")
        else:
            assert parent.status == "cleared"
            assert child is not None
            child.mutation_id = before.mutation_id  # every durable write gets a fresh token
            assert child.to_json() == before.to_json()
        assert not adapter._background_tasks  # migration never replays the in-flight tick
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.parametrize("control", ["pause", "clear", "revise"])
def test_compression_loop_migration_cas_keeps_concurrent_parent_control(control_home, monkeypatch, control):
    mgr = loops.LoopManager("parent")
    mgr.set("check work", interval_seconds=60)
    assert mgr.fire_tick(7)
    load = loops.load_loop
    changed = False
    controlled = None

    def load_and_control(sid, **kwargs):
        nonlocal changed, controlled
        result = load(sid, **kwargs)
        if sid == "parent" and not kwargs and not changed:
            changed = True
            fresh = loops.LoopManager(sid)
            if control == "revise":
                assert fresh.revise(reason="slow down", interval_seconds=120)["ok"]
            else:
                getattr(fresh, control)()
            controlled = load(sid).to_json()
        return result

    monkeypatch.setattr(loops, "load_loop", load_and_control)
    assert loops.migrate_loop_to_session("parent", "child", reason="compression") is False
    assert load("child") is None
    assert load("parent").to_json() == controlled


def test_compression_loop_migration_rolls_back_child_when_parent_retirement_fails(control_home, monkeypatch):
    mgr = loops.LoopManager("parent")
    mgr.set("check work", interval_seconds=60)
    assert mgr.fire_tick(7)
    before = loops.load_loop("parent").to_json()
    db = loops._get_session_db()
    set_meta = db.set_meta

    def fail_retirement(key, value, **kwargs):
        if key == "loop:parent":
            raise RuntimeError("retirement write unavailable")
        return set_meta(key, value, **kwargs)

    monkeypatch.setattr(db, "set_meta", fail_retirement)
    assert loops.migrate_loop_to_session("parent", "child", reason="compression") is False
    assert loops.load_loop("child") is None
    assert loops.load_loop("parent").to_json() == before
