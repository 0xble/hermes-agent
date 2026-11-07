"""Quiet /loop wakeups through the real gateway fire path and response boundary.

A gateway /loop tick is fired by ``_loop_wakeup_fire_one`` from a real ``LoopManager``. The injected
event must be internal with ``reply_expected=False`` and carry a prompt that asks for a bare
``[SILENT]`` on a no-change tick; that event's ``[SILENT]`` reply is then suppressed at delivery
while a changed result is still delivered.
"""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.response_filters import display_kind_for_event
from gateway.run import GatewayRunner
from gateway.run_turn import GatewayTurnMixin
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals
    from hermes_state_registry import close_all_under

    goals._DB_CACHE.clear()
    # Open the store up front: under load the lazy bootstrap can miss its grace window, leaving loop
    # writes unpersisted while the wake fence reads the store.
    from hermes_state_registry import acquire
    goals._DB_CACHE[str(home)] = acquire(home / "state.db")
    yield home
    goals._DB_CACHE.clear()
    close_all_under(home)


class _Adapter:
    def __init__(self):
        self.events = []

    async def handle_message(self, event):
        self.events.append(event)
        event._gateway_accepted = True  # the admission receipt a real adapter returns


def _runner(adapter):
    r = object.__new__(GatewayRunner)
    r.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")})
    r.adapters = {Platform.TELEGRAM: adapter}
    r._profile_adapters = {}
    r._primary_profile_name = "default"
    r.session_store = None
    r._session_sources = None
    r._running_agents = {}
    r._running = True

    async def _inline(func, *args):
        return func(*args)

    r._run_in_executor_with_context = _inline
    return r


@pytest.fixture()
def goal_loop(hermes_home, monkeypatch):
    """A real persisted goal/loop pair using the gateway wake fixtures and a fixed clock."""
    from hermes_cli import goals, loops

    clock = SimpleNamespace(now=2_000_000_000.0)
    monkeypatch.setattr(loops.time, "time", lambda: clock.now)

    def setup(interval_seconds=300):
        sid = "loop-goal-sid"
        route = {"platform": "telegram", "chat_id": "42", "chat_type": "dm", "user_id": "42"}
        goal = goals.GoalManager(session_id=sid)
        goal.set("finish the migration")
        loop = loops.LoopManager(session_id=sid)
        loop.set("check the queue", interval_seconds=interval_seconds or None, route=route)
        assert loop.state is not None
        assert loop.state.mode == ("interval" if interval_seconds else "self_paced")
        interval = interval_seconds or loops.self_paced_floor_seconds()
        if not interval_seconds:
            # A quiet self-paced loop may already be at its ceiling. Goal deferral still uses
            # the floor, not another full backoff period on top of its scheduled delay.
            loop.state.current_delay = float(loops.self_paced_ceiling_seconds())
            loop._save()
        adapter = _Adapter()
        runner = _runner(adapter)
        runner._adapters_for_profile = lambda profile: {Platform.TELEGRAM: adapter}
        runner._build_process_event_source = lambda evt: SessionSource(
            platform=Platform.TELEGRAM, chat_id=evt["chat_id"], chat_type=evt["chat_type"],
            user_id=evt["user_id"],
        )
        runner._session_key_for_source = lambda source: "agent:main:telegram:dm:42"
        return SimpleNamespace(
            sid=sid, goal=goal, loop=loop, interval=interval, clock=clock,
            runner=runner, adapter=adapter,
        )

    return setup


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_seconds", [300, 0], ids=["fixed", "self-paced"])
@pytest.mark.parametrize("overdue_intervals", [1, 2], ids=["at-limit", "past-limit"])
async def test_idle_active_goal_cannot_starve_overdue_loop(goal_loop, interval_seconds, overdue_intervals):
    from hermes_cli import goals, loops

    env = goal_loop(interval_seconds)
    env.loop.state.next_due_at = env.clock.now - overdue_intervals * env.interval
    env.loop._save()
    assert env.goal.is_active() and not env.goal.is_waiting()

    await env.runner._loop_wakeup_fire_one(env.sid, env.loop.state, env.clock.now, set())

    assert len(env.adapter.events) == 1
    stored = loops.load_loop(env.sid)
    assert stored is not None
    assert stored.ticks_fired == 1 and stored.awaiting_response
    goal = goals.GoalManager(session_id=env.sid).state
    assert goal is not None and goal.turns_used == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_seconds", [300, 0], ids=["fixed", "self-paced"])
@pytest.mark.parametrize("overdue_fraction", [0, 0.99], ids=["just-due", "before-limit"])
async def test_active_goal_keeps_initial_loop_deferral(goal_loop, interval_seconds, overdue_fraction):
    from hermes_cli import loops

    env = goal_loop(interval_seconds)
    env.loop.state.next_due_at = env.clock.now - overdue_fraction * env.interval
    env.loop._save()
    assert env.loop.is_due(env.clock.now)

    await env.runner._loop_wakeup_fire_one(env.sid, env.loop.state, env.clock.now, set())

    assert env.adapter.events == []
    stored = loops.load_loop(env.sid)
    assert stored is not None
    assert stored.ticks_fired == 0 and not stored.awaiting_response
    assert stored.next_due_at == env.loop.state.next_due_at


@pytest.mark.asyncio
async def test_overdue_loop_still_defers_to_inflight_goal_turn(goal_loop):
    from hermes_cli import loops

    env = goal_loop()
    env.loop.state.next_due_at = env.clock.now - 2 * env.interval
    env.loop._save()
    key = env.runner._session_key_for_source(None)
    env.runner._running_agents[key] = SimpleNamespace(goal_continuation=True)

    await env.runner._loop_wakeup_fire_one(env.sid, env.loop.state, env.clock.now, set())

    assert env.adapter.events == []
    stored = loops.load_loop(env.sid)
    assert stored is not None
    assert stored.ticks_fired == 0 and not stored.awaiting_response

    env.runner._running_agents.pop(key)
    await env.runner._loop_wakeup_fire_one(env.sid, stored, env.clock.now, set())
    assert len(env.adapter.events) == 1


@pytest.mark.asyncio
async def test_parked_goal_with_holding_barrier_does_not_defer_loop(goal_loop):
    from hermes_cli import goals, loops

    env = goal_loop()
    env.goal.wait_for_seconds(3600, reason="waiting on CI")
    assert env.goal.is_active() and env.goal.is_waiting()

    await env.runner._loop_wakeup_fire_one(env.sid, env.loop.state, env.clock.now, set())

    assert len(env.adapter.events) == 1
    stored = loops.load_loop(env.sid)
    assert stored is not None and stored.ticks_fired == 1
    assert goals.GoalManager(session_id=env.sid).is_waiting()


@pytest.mark.asyncio
async def test_goal_loop_deferral_logs_once_per_interval(goal_loop, caplog):
    import logging

    from hermes_cli import loops

    env = goal_loop()
    caplog.set_level(logging.INFO, logger="gateway.run")
    for elapsed in (0, 1, env.interval - 1):
        env.clock.now = env.loop.state.next_due_at + elapsed
        await env.runner._loop_wakeup_fire_one(env.sid, env.loop.state, env.clock.now, set())

    def deferred_records():
        return [record for record in caplog.records if "deferred by active goal" in record.message]

    assert len(deferred_records()) == 1
    assert deferred_records()[0].levelno == logging.INFO
    assert env.sid in deferred_records()[0].message
    assert env.adapter.events == []

    env.clock.now = env.loop.state.next_due_at + env.interval
    await env.runner._loop_wakeup_fire_one(env.sid, env.loop.state, env.clock.now, set())
    assert len(env.adapter.events) == 1
    assert len(deferred_records()) == 1

    loop = loops.LoopManager(session_id=env.sid)
    loop.complete_tick("[SILENT]")
    assert loop.state is not None
    env.clock.now = loop.state.next_due_at
    await env.runner._loop_wakeup_fire_one(env.sid, loop.state, env.clock.now, set())
    assert len(deferred_records()) == 2
    assert len(env.adapter.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_seconds", [300, 0], ids=["fixed", "self-paced"])
async def test_full_background_gate_deferral_is_logged_once_per_interval(
    goal_loop, caplog, monkeypatch, interval_seconds,
):
    import logging

    from hermes_cli import loops

    env = goal_loop(interval_seconds)
    env.goal.wait_for_seconds(3_600, reason="waiting on CI")
    occupied = {f"busy-{n}" for n in range(env.runner.config.background_turn_concurrency)}
    monkeypatch.setattr(env.runner, "_background_turn_keys", lambda: occupied)
    gate = env.runner._get_replay_scheduler().gate
    assert gate.load() == gate.limit == 8
    due_at = env.loop.state.next_due_at

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        for elapsed in (0, 1, env.interval - 1, env.interval):
            env.clock.now = due_at + elapsed
            await env.runner._loop_wakeup_fire_one(env.sid, env.loop.state, env.clock.now, set())

    records = [record for record in caplog.records if "deferred by full background gate" in record.message]
    assert len(records) == 2, "a full gate should be visible without logging every scan"
    assert all(record.levelno == logging.INFO and env.sid in record.message for record in records)
    assert env.adapter.events == []
    unchanged = loops.load_loop(env.sid)
    assert unchanged is not None
    assert unchanged.ticks_fired == 0
    assert unchanged.next_due_at == due_at
    assert unchanged.awaiting_response is False

    occupied.clear()
    await env.runner._loop_wakeup_fire_one(env.sid, env.loop.state, env.clock.now, set())
    assert len(env.adapter.events) == 1, "the unclaimed tick stays eligible when capacity returns"
    fired = loops.load_loop(env.sid)
    assert fired is not None and fired.ticks_fired == 1


class _ShapeRunner(GatewayTurnMixin):
    def __init__(self):
        self.async_session_store = SimpleNamespace(clear_resume_pending=self._noop)

    async def _noop(self, *_args, **_kwargs):
        return None

    async def _clear_restart_failure_count(self, *_args, **_kwargs):
        return None


def _stub_gateway_run(monkeypatch):
    module = ModuleType("gateway.run")
    module._is_gateway_hidden_reasoning_incomplete_turn = lambda _result: False
    module._normalize_empty_agent_response = lambda _result, response, history_len: response
    module._sanitize_gateway_final_response = lambda _platform, response, interrupted: response
    module._should_clear_resume_pending_after_turn = lambda _result: False
    monkeypatch.setitem(sys.modules, "gateway.run", module)


async def _deliver(event, final_response, monkeypatch):
    """Shape and deliver ``final_response`` for ``event`` as the turn path does, deriving the
    silence inputs (display kind, reply_expected) from the fired event itself."""
    _stub_gateway_run(monkeypatch)
    runner = _ShapeRunner()
    session = SimpleNamespace(session_id="loop-session", incognito=False)
    shaped, marker, _ = await runner._hmwa_shape_agent_response(
        {"final_response": final_response, "messages": [], "api_calls": 1},
        event.source, history=[], session_entry=session, session_key=None,
        _quick_key=None, run_generation=0, _run_start_session_id="loop-session",
        _platform_name="telegram", _msg_start_time=0.0,
        persist_user_display_kind=display_kind_for_event(event), reply_expected=event.reply_expected,
    )
    runner._delivery_adapter_for = lambda _source: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    return await runner._hmwa_deliver_turn_response(
        event, event.source, session, None, 0,
        {"final_response": final_response}, [], shaped, None, marker,
    )


@pytest.mark.asyncio
async def test_fired_loop_wakeup_asks_for_silence_and_its_silent_reply_is_not_delivered(
    hermes_home, monkeypatch,
):
    from hermes_cli.loops import LoopManager

    route = {"platform": "telegram", "chat_id": "42", "chat_type": "dm", "user_id": "42"}
    mgr = LoopManager(session_id="loop-sid")
    mgr.set("check the queue", interval_seconds=300, route=route)
    mgr.state.next_due_at = 0
    mgr._save()

    adapter = _Adapter()
    state = LoopManager(session_id="loop-sid").state
    if state is None:
        # The loop DB writer is intentionally asynchronous under the test/bootstrap path; the
        # gateway's scanner would already hold the state it loaded, so use this manager's state.
        state = mgr.state
    from hermes_cli import loops
    monkeypatch.setattr(loops, "LoopManager", lambda session_id: mgr)
    runner = _runner(adapter)
    runner._adapters_for_profile = lambda profile: {Platform.TELEGRAM: adapter}
    runner._build_process_event_source = lambda evt: SessionSource(
        platform=Platform.TELEGRAM, chat_id=evt["chat_id"], chat_type=evt["chat_type"],
        user_id=evt["user_id"], user_name=evt.get("user_name"),
    )
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:42"
    runner._running_agents = {}
    runner._run_in_executor_with_context = _runner(adapter)._run_in_executor_with_context
    await runner._loop_wakeup_fire_one("loop-sid", state, 1e12, set())

    assert len(adapter.events) == 1
    event = adapter.events[0]
    assert event.internal is True
    assert event.reply_expected is False
    assert "reply with exactly [SILENT] and nothing else" in event.text
    assert "No Evidence section" in event.text
    assert LoopManager(session_id="loop-sid").state.awaiting_response is True

    assert await _deliver(event, "[SILENT]", monkeypatch) == ""
    combo = "[SILENT]" + chr(10) + "LOOP_COMPLETE"
    assert await _deliver(event, combo, monkeypatch) == ""
    assert event._raw_final_response == combo
    assert await _deliver(event, "Queue depth changed to 2.", monkeypatch) == "Queue depth changed to 2."


@pytest.mark.asyncio
async def test_loop_wakeup_control_suffix_is_suppressed_but_raw_loop_stop_survives(
    hermes_home, monkeypatch,
):
    event = GatewayRunner._synthetic_prompt_event(
        SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm", user_id="42"),
        "[loop wakeup]" + chr(10) + "Check the queue",
        reply_expected=False,
    )
    event.internal = True
    assert await _deliver(event, "[SILENT]" + chr(10) + "LOOP_COMPLETE", monkeypatch) == ""
    assert await _deliver(event, "NO_REPLY" + chr(10) + "LOOP_COMPLETE", monkeypatch) == ""
    assert await _deliver(event, "LOOP_COMPLETE", monkeypatch) == ""
    assert await _deliver(
        event, "Queue depth changed to 2." + chr(10) + "LOOP_COMPLETE", monkeypatch,
    ) == "Queue depth changed to 2."


@pytest.mark.asyncio
async def test_human_loop_complete_silence_gets_existing_fallback(hermes_home, monkeypatch):
    event = MessageEvent(
        text="typed request",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm", user_id="42"),
    )
    reply = await _deliver(event, "[SILENT]" + chr(10) + "LOOP_COMPLETE", monkeypatch)
    assert "No reply was written" in reply


@pytest.mark.asyncio
async def test_goal_continuation_silence_is_gateway_quiet_but_updates_deliver(
    hermes_home, monkeypatch,
):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm", user_id="42")
    event = GatewayRunner._synthetic_prompt_event(
        source, "[Continuing toward your standing goal]\\nGoal: check the queue", reply_expected=False,
    )
    assert event.reply_expected is False
    assert await _deliver(event, "[SILENT]", monkeypatch) == ""
    assert await _deliver(event, "Queue depth changed to 2.", monkeypatch) == "Queue depth changed to 2."


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["stop", "pause", "reset"])
async def test_fired_loop_wake_is_dropped_once_its_tick_is_no_longer_running(
    hermes_home, monkeypatch, control,
):
    """A stop or pause approved after the tick was claimed must not start the obsolete prompt."""
    from hermes_cli.loops import LoopManager
    from hermes_cli.session_controls import internal_wake_is_current, loop_wake_is_current

    route = {"platform": "telegram", "chat_id": "42", "chat_type": "dm", "user_id": "42"}
    mgr = LoopManager(session_id="loop-sid")
    mgr.set("check the queue", interval_seconds=300, route=route)
    mgr.state.next_due_at = 0
    mgr._save()

    adapter = _Adapter()
    from hermes_cli import loops
    monkeypatch.setattr(loops, "LoopManager", lambda session_id: mgr)
    runner = _runner(adapter)
    runner._adapters_for_profile = lambda profile: {Platform.TELEGRAM: adapter}
    runner._build_process_event_source = lambda evt: SessionSource(
        platform=Platform.TELEGRAM, chat_id=evt["chat_id"], chat_type=evt["chat_type"],
        user_id=evt["user_id"], user_name=evt.get("user_name"),
    )
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:42"
    await runner._loop_wakeup_fire_one("loop-sid", mgr.state, 1e12, set())
    event = adapter.events[0]
    assert loop_wake_is_current(event.metadata, "loop-sid") is True
    assert internal_wake_is_current(event, "loop-sid") is True

    real = type(mgr)(session_id="loop-sid")
    if control == "stop":
        real.clear()
    elif control == "pause":
        real.pause("approved in Telegram")
    else:
        real.set("a different loop", interval_seconds=300, route=route)
    assert loop_wake_is_current(event.metadata, "loop-sid") is False
    assert internal_wake_is_current(event, "loop-sid") is False


@pytest.mark.asyncio
async def test_busy_loop_tick_defers_until_active_turn_releases_session(hermes_home, monkeypatch):
    """A due loop never claims or bumps the run while another turn owns the session."""
    import asyncio

    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.platforms.event import MessageType
    from hermes_cli.loops import LoopManager, load_loop

    class ActiveAdapter(BasePlatformAdapter):
        def __init__(self):
            super().__init__(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)
            self.sent = []

        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            pass

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append(content)
            return SendResult(success=True)

        async def get_chat_info(self, chat_id):
            return {"id": chat_id, "type": "dm"}

    route = {"platform": "telegram", "chat_id": "42", "chat_type": "dm", "user_id": "42"}
    mgr = LoopManager(session_id="loop-sid")
    mgr.set("/status", interval_seconds=300, route=route, times=1)
    mgr.state.next_due_at = 0
    mgr._save()

    adapter = ActiveAdapter()
    handled = []
    events = []

    async def handler(event):
        events.append(event)
        handled.append(event.get_command())
        event._gateway_inline_command_handled = True
        event._gateway_accepted = True
        return "status: busy"

    adapter.set_message_handler(handler)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm", user_id="42")
    key = adapter._event_session_key(MessageEvent(text="", message_type=MessageType.TEXT, source=source))
    runner_owner = object()  # installed below once the runner exists

    from hermes_cli import loops
    monkeypatch.setattr(loops, "LoopManager", lambda session_id: mgr)
    runner = _runner(adapter)
    runner._adapters_for_profile = lambda profile: {Platform.TELEGRAM: adapter}
    runner._build_process_event_source = lambda evt: SessionSource(
        platform=Platform.TELEGRAM, chat_id=evt["chat_id"], chat_type=evt["chat_type"],
        user_id=evt["user_id"], user_name=evt.get("user_name"),
    )
    runner._session_key_for_source = lambda source: key
    runner._running_agents[key] = runner_owner
    generation = runner._begin_session_run_generation(key)
    await runner._loop_wakeup_fire_one("loop-sid", mgr.state, 1e12, set())

    stored = load_loop("loop-sid")
    assert handled == []
    assert stored.ticks_fired == 0 and not stored.awaiting_response
    assert runner._current_session_run_generation(key) == generation

    runner._running_agents.pop(key)
    await runner._loop_wakeup_fire_one("loop-sid", stored, 1e12, set())
    await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    assert handled == ["status"]
    assert adapter.sent == ["status: busy"]
    stored = load_loop("loop-sid")
    assert stored.ticks_fired == 1
    assert events and events[0]._gateway_accepted is True and events[0]._gateway_inline_command_handled is True


def test_untagged_internal_events_are_not_loop_fenced():
    from hermes_cli.session_controls import loop_wake_is_current

    assert loop_wake_is_current({}, "x") is True
    assert loop_wake_is_current(None, "x") is True
