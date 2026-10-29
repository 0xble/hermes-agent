"""Gateway /loop command tests — dispatch, routing capture, mid-run guard."""

import asyncio
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals, loops


class _FakeSessionEntry:
    session_id = "sid-gateway-loop"


class _FakeSessionStore:
    def __init__(self):
        self.entry = _FakeSessionEntry()

    def get_or_create_session(self, source, *, touch_activity=True):
        return self.entry

    def _generate_session_key(self, source):
        return "agent:main:discord:channel:loop-test"


@pytest.fixture
def loop_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    # Pre-warm the SessionDB cache from this sync (non-loop) context. Inside
    # the async tests, a cold cache makes GoalManager.set() kick the bounded
    # background bootstrap (loop-thread path) and wait only
    # _DB_BOOTSTRAP_INIT_WAIT_S — on a loaded CI runner the init overruns the
    # window, the goal is never persisted, and the active-goal assertion
    # flakes (main run 33455779041). Warming here removes the race entirely.
    goals._get_session_db()
    yield home
    goals._DB_CACHE.clear()


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}
    return runner


def _loop_wake_event(mgr) -> MessageEvent:
    """The stamped wake the scheduler injects for the tick ``mgr`` just fired."""
    from hermes_cli.session_controls import loop_wake_metadata
    event = _make_event("wakeup")
    event.internal = True
    event.metadata = loop_wake_metadata(mgr.session_id, mgr.state)
    return event


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-loop",
            chat_type="channel",
            thread_id="thread-9",
            user_id="user-loop",
        ),
        message_id="msg-loop",
    )


@pytest.mark.asyncio
async def test_gateway_loop_create_captures_route(loop_env):
    runner = _make_runner()
    response = await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m check the deploy"))
    assert "Loop set" in response
    assert "every 5m" in response

    state = loops.load_loop("sid-gateway-loop")
    assert state is not None
    assert state.prompt == "check the deploy"
    assert state.route["platform"] == "discord"
    assert state.route["chat_id"] == "chat-loop"
    assert state.route["thread_id"] == "thread-9"


@pytest.mark.asyncio
async def test_gateway_loop_status_pause_stop(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    status = await GatewayRunner._handle_loop_command(runner, _make_event("/loop status"))
    assert "poll CI" in status

    paused = await GatewayRunner._handle_loop_command(runner, _make_event("/loop pause"))
    assert "paused" in paused.lower()

    stopped = await GatewayRunner._handle_loop_command(runner, _make_event("/loop stop"))
    assert "stopped" in stopped.lower()




@pytest.mark.asyncio
# health: allow F841 -- loop wake completion tests intentionally retain the local manager for exact tick identity assertions.
async def test_post_turn_loop_completion_completes_inflight_tick(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    assert mgr.state is not None
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None
    assert mgr.state is not None

    entry = _FakeSessionEntry()
    await GatewayRunner._post_turn_loop_completion(
        runner,
        session_entry=entry,
        source=None,
        final_response="CI is done.\nLOOP_COMPLETE",
        loop_tick=(mgr.state.created_at, mgr.state.ticks_fired),
    )
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.status == "done"


@pytest.mark.asyncio
async def test_post_turn_completion_does_not_stop_a_loop_revised_during_the_turn(loop_env):
    """The gateway completes with a fresh manager: a stop verdict on prompt A must not end B."""
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll prompt A"))
    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    assert mgr.state is not None
    mgr.state.next_due_at = time.time() - 1
    loops.save_loop("sid-gateway-loop", mgr.state)
    fired = loops.LoopManager(session_id="sid-gateway-loop")
    assert fired.fire_tick() is not None
    tick = (fired.state.created_at, fired.state.ticks_fired)
    revised = loops.LoopManager(session_id="sid-gateway-loop").revise(
        reason="new target", prompt="poll prompt B",
        user_quote="switch the loop to polling prompt B",
        user_messages=["Please switch the loop to polling prompt B now"])
    assert revised["ok"] is True, revised

    await GatewayRunner._post_turn_loop_completion(
        runner, session_entry=_FakeSessionEntry(), source=None,
        final_response="Prompt A is done.\nLOOP_COMPLETE", loop_tick=tick,
    )
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.status == "active" and reloaded.prompt == "poll prompt B"
    assert reloaded.awaiting_response is False


@pytest.mark.asyncio
async def test_post_turn_loop_completion_noop_without_inflight_tick(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))
    entry = _FakeSessionEntry()
    # No tick fired — the ordinary user turn must not consume loop state.
    await GatewayRunner._post_turn_loop_completion(
        runner,
        session_entry=entry,
        source=None,
        final_response="regular reply LOOP_COMPLETE",
    )
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.status == "active"
    assert reloaded.ticks_fired == 0


def test_streamed_already_sent_none_recovers_text_for_hooks():
    """Streamed turns return None. Hooks must still see the delivered reply."""
    event = _make_event("wakeup")
    event._streamed_final_response = "CI is green.\nLOOP_COMPLETE"
    assert GatewayRunner._final_text_for_post_turn_hooks(None, event) == (
        "CI is green.\nLOOP_COMPLETE"
    )
    assert GatewayRunner._final_text_for_post_turn_hooks(None, _make_event("x")) == ""
    assert (
        GatewayRunner._final_text_for_post_turn_hooks(
            {"final_response": "from dict"}, event
        )
        == "from dict"
    )


@pytest.mark.asyncio
async def test_non_streamed_stripped_reply_still_completes_loop_tick(loop_env):
    """Non-streamed delivery returns display text with LOOP_COMPLETE stripped; the post-turn
    hook must read the raw reply stashed on the event so the loop still finishes."""
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))
    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    assert mgr.state is not None
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None
    assert mgr.state is not None

    event = _make_event("wakeup")
    event._raw_final_response = "CI is done.\nLOOP_COMPLETE"
    # _handle_message_with_agent returned the stripped display text.
    final_text = GatewayRunner._final_text_for_post_turn_hooks("CI is done.", event)
    assert final_text == "CI is done.\nLOOP_COMPLETE"

    await GatewayRunner._post_turn_loop_completion(
        runner, session_entry=_FakeSessionEntry(), source=None, final_response=final_text,
        loop_tick=(mgr.state.created_at, mgr.state.ticks_fired),
    )
    assert loops.load_loop("sid-gateway-loop").status == "done"


@pytest.mark.asyncio
async def test_streamed_already_sent_completes_loop_tick(loop_env):
    """A streamed wakeup must not leave awaiting_response stuck."""
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    assert mgr.state is not None
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None
    assert mgr.state is not None
    assert mgr.state.awaiting_response is True
    assert mgr.is_due() is False

    event = _make_event("wakeup")
    event._streamed_final_response = "CI is done.\nLOOP_COMPLETE"
    # Same inputs the already_sent branch leaves for _handle_message.
    final_text = GatewayRunner._final_text_for_post_turn_hooks(None, event)
    assert final_text.strip()

    await GatewayRunner._post_turn_loop_completion(
        runner,
        session_entry=_FakeSessionEntry(),
        source=None,
        final_response=final_text,
        loop_tick=(mgr.state.created_at, mgr.state.ticks_fired),
    )
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.awaiting_response is False
    assert reloaded.status == "done"


@pytest.mark.asyncio
async def test_loop_tick_is_not_reclassified_as_external_evidence(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    assert mgr.state is not None
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None
    event = _loop_wake_event(mgr)
    runner._post_turn_goal_continuation = AsyncMock()
    with patch("hermes_cli.loops.wake_loop_for_external_event") as wake:
        await GatewayRunner._run_post_turn_hooks(
            runner,
            agent_result={"final_response": "still working"},
            source=event.source,
            is_internal=True,
            event=event,
        )
    wake.assert_not_called()


@pytest.mark.asyncio
async def test_trusted_external_result_wakes_loop_once(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))
    event = _make_event("result")
    event.internal = True
    event.metadata = {"notification_origin": "delegation_result", "notification_category": "result"}
    runner._post_turn_goal_continuation = AsyncMock()
    with patch("hermes_cli.loops.wake_loop_for_external_event", return_value=True) as wake:
        await GatewayRunner._run_post_turn_hooks(
            runner,
            agent_result={"final_response": "delegation completed"},
            source=event.source,
            is_internal=True,
            event=event,
        )
    wake.assert_called_once_with("sid-gateway-loop")


@pytest.mark.asyncio
async def test_empty_agent_result_releases_inflight_loop_tick(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    assert mgr.state is not None
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None
    assert mgr.state is not None
    assert mgr.state.awaiting_response is True

    event = _make_event("wakeup")
    event.metadata["gateway_loop_tick"] = [mgr.state.created_at, mgr.state.ticks_fired]
    runner._post_turn_goal_continuation = AsyncMock()
    await GatewayRunner._run_post_turn_hooks(
        runner,
        agent_result={"final_response": ""},
        source=event.source,
        is_internal=True,
        event=_loop_wake_event(mgr),
    )

    runner._post_turn_goal_continuation.assert_not_awaited()
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.awaiting_response is False
    assert reloaded.status == "active"
    assert reloaded.next_due_at > time.time()


@pytest.mark.asyncio
async def test_goal_hook_failure_does_not_block_loop_completion(loop_env, caplog):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    assert mgr.state is not None
    mgr.state.next_due_at = time.time() - 1
    assert mgr.fire_tick() is not None

    event = _make_event("wakeup")
    assert mgr.state is not None
    event.metadata["gateway_loop_tick"] = [mgr.state.created_at, mgr.state.ticks_fired]
    runner._post_turn_goal_continuation = AsyncMock(side_effect=RuntimeError("judge failed"))
    with caplog.at_level(logging.DEBUG, logger="gateway.run"):
        await GatewayRunner._run_post_turn_hooks(
            runner,
            agent_result={"final_response": "still working"},
            source=event.source,
            is_internal=True,
            event=_loop_wake_event(mgr),
        )

    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.awaiting_response is False






@pytest.mark.asyncio
async def test_loop_wakeup_watcher_runs_every_sessiondb_call_off_loop_thread(loop_env):
    """Full wakeup path (slash-command loop, adapter present): list_active_loops,
    fire_tick and complete_tick must each execute on an executor thread, never
    on the event-loop thread (#92413)."""
    runner = _make_runner()
    runner._running = True
    runner._running_agents = {}

    class _Adapter:
        handled = []

        async def handle_message(self, event):
            self.handled.append(event.text)
            event._gateway_accepted = True  # the admission receipt a real adapter returns

    runner.adapters = {Platform.DISCORD: _Adapter()}
    runner._build_process_event_source = lambda evt: SimpleNamespace(
        platform=Platform.DISCORD, chat_id=evt["chat_id"], chat_type=evt["chat_type"],
        thread_id=evt["thread_id"] or None, user_id=evt["user_id"], user_name=evt["user_name"],
    )
    runner._session_key_for_source = lambda source: "agent:main:discord:channel:chat-loop"

    # A slash-command loop that is due now.
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m /status"))
    state = loops.load_loop("sid-gateway-loop")
    state.next_due_at = time.time() - 1
    loops.save_loop("sid-gateway-loop", state)

    loop_thread = threading.current_thread()
    on_loop_calls = []
    seen_calls = []

    def _record(name):
        # Positive count guards against a future import hoist in run.py that
        # would bypass these wrappers and leave the off-loop assertion vacuous.
        seen_calls.append(name)
        if threading.current_thread() is loop_thread:
            on_loop_calls.append(name)

    real_list = loops.list_active_loops
    real_fire = loops.LoopManager.fire_tick
    real_complete = loops.LoopManager.complete_tick

    def _list_active_loops(*a, **k):
        _record("list_active_loops")
        return real_list(*a, **k)

    def _fire_tick(self, *args, **kwargs):
        _record("fire_tick")
        return real_fire(self, *args, **kwargs)

    def _complete_tick(self, last_response, *, tick=None):
        _record("complete_tick")
        return real_complete(self, last_response, tick=tick)

    orig_sleep = asyncio.sleep
    calls = {"n": 0}

    async def _one_pass_sleep(delay):
        calls["n"] += 1
        if calls["n"] >= 2:
            runner._running = False
        return await orig_sleep(0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loops, "list_active_loops", _list_active_loops)
        mp.setattr(loops.LoopManager, "fire_tick", _fire_tick)
        mp.setattr(loops.LoopManager, "complete_tick", _complete_tick)
        mp.setattr(asyncio, "sleep", _one_pass_sleep)
        await GatewayRunner._loop_wakeup_watcher(runner, interval=0)

    assert _Adapter.handled == ["/status"], _Adapter.handled
    assert seen_calls == ["list_active_loops", "fire_tick", "complete_tick"], seen_calls
    assert on_loop_calls == [], f"SessionDB calls ran on the event-loop thread: {on_loop_calls}"
    # complete_tick ran (slash-command loops complete immediately).
    assert loops.load_loop("sid-gateway-loop").ticks_fired == 1


@pytest.mark.asyncio
async def test_loop_wakeup_watcher_scans_owning_home_without_pre_admission_hydration(loop_env, monkeypatch, tmp_path):
    """Loop discovery reads the owning store without hydrating secrets before tick admission."""
    from hermes_state import SessionDB

    work_home = tmp_path / "profiles" / "work"
    work_home.mkdir(parents=True)
    work_db = SessionDB(db_path=work_home / "state.db")
    goals._DB_CACHE[str(work_home)] = work_db

    scopes = [(None, None), ("work", work_home)]
    on_main_thread = []

    def _scopes(_runner):
        # profiles_to_serve() walks the filesystem: it must resolve off the loop thread.
        on_main_thread.append(threading.current_thread() is threading.main_thread())
        return scopes

    monkeypatch.setattr("gateway.run._handoff_watch_scopes", _scopes)

    entered = []

    class _SpyScope:
        def __init__(self, home):
            self.home = home

        async def __aenter__(self):
            entered.append(self.home)
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("gateway.run._async_profile_runtime_scope", _SpyScope)

    runner = _make_runner()
    runner.config.multiplex_profiles = True
    runner._running_agents = {}
    runner.adapters = {}
    from hermes_constants import get_hermes_home
    admitted = []

    async def admission(sid, state, now, warned, profile=None):
        admitted.append((sid, get_hermes_home(), profile))

    runner._loop_wakeup_fire_one = admission

    orig_sleep = asyncio.sleep

    async def _run_one_tick():
        runner._running = True
        calls = {"n": 0}

        async def _one_pass_sleep(delay):
            calls["n"] += 1
            if calls["n"] >= 2:  # 5s connect grace, then exactly one scan pass
                runner._running = False
            return await orig_sleep(0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _one_pass_sleep)
            await asyncio.wait_for(
                GatewayRunner._loop_wakeup_watcher(runner, interval=0), timeout=5)

    try:
        # Idle: no loop rows in the work profile's store → its scope is never entered.
        await _run_one_tick()
        assert entered == [], f"idle profile scope must not be entered; got {entered}"
        assert on_main_thread == [False], f"scopes must resolve off the loop; got {on_main_thread}"

        # An ACTIVE loop row in the work profile's store opens the gate.
        await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))
        raw = loops._get_session_db().get_meta("loop:sid-gateway-loop")
        assert raw
        work_db.set_meta("loop:sid-work-loop", raw)
        await _run_one_tick()
        assert entered == [], f"secret hydration preceded admission: {entered}"
        assert admitted == [
            ("sid-gateway-loop", loop_env, None),
            ("sid-work-loop", work_home, "work"),
        ]

        # A cleared loop keeps its row (status=cleared): the gate must read status, not key existence.
        entered.clear()
        cleared = loops.LoopState.from_json(raw)
        cleared.status = "cleared"
        work_db.set_meta("loop:sid-work-loop", cleared.to_json())
        admitted.clear()
        await _run_one_tick()
        assert entered == [], f"cleared loop row must not open the gate; got {entered}"
        assert admitted == [("sid-gateway-loop", loop_env, None)]

        # Fail OPEN: a store the probe cannot open is "unknown", never "idle" — the scan runs.
        monkeypatch.setattr("gateway.run_idle_gates._profile_session_db_probe", lambda _home: None)
        await _run_one_tick()
        assert entered == [], f"an unavailable store triggered pre-admission hydration: {entered}"
    finally:
        work_db.close()


@pytest.mark.asyncio
async def test_post_turn_completion_judges_only_the_tick_its_wake_carried(loop_env):
    """A stop, re-set and new tick before A's hooks run: A's LOOP_COMPLETE must not finish B."""
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll prompt A"))
    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    assert mgr.state is not None
    mgr.state.next_due_at = time.time() - 1
    loops.save_loop("sid-gateway-loop", mgr.state)
    fired_a = loops.LoopManager(session_id="sid-gateway-loop")
    assert fired_a.fire_tick() is not None
    tick_a = (fired_a.state.created_at, fired_a.state.ticks_fired)

    loops.LoopManager(session_id="sid-gateway-loop").clear()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll prompt B"))
    b = loops.LoopManager(session_id="sid-gateway-loop")
    b.state.next_due_at = time.time() - 1
    loops.save_loop("sid-gateway-loop", b.state)
    assert loops.LoopManager(session_id="sid-gateway-loop").fire_tick() is not None

    await GatewayRunner._post_turn_loop_completion(
        runner, session_entry=_FakeSessionEntry(), source=None,
        final_response="Prompt A is done.\nLOOP_COMPLETE", loop_tick=tick_a,
    )
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.status == "active" and reloaded.prompt == "poll prompt B"
    assert reloaded.awaiting_response is True  # B's own tick is still in flight


def test_post_turn_identity_carries_the_loop_tick_from_the_wake():
    from gateway.platforms.event import MessageEvent, MessageType
    from hermes_cli.session_controls import loop_wake_metadata

    runner = _make_runner()
    state = loops.LoopState(prompt="p", interval_seconds=300, created_at=123.0, ticks_fired=4)
    event = MessageEvent(text="tick", message_type=MessageType.TEXT, source=_make_event("x").source,
                         internal=True, metadata=loop_wake_metadata("sid", state))
    assert GatewayRunner._post_turn_goal_identity(runner, event)["loop_tick"] == [123.0, 4]


@pytest.mark.asyncio
async def test_unstamped_turn_does_not_settle_a_tick_this_gateway_claimed(loop_env):
    """A human turn ending while a new tick is in flight must leave that tick to its own wake."""
    runner = _make_runner()
    runner._gateway_started_at = time.time() - 60
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))
    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    assert mgr.state is not None
    mgr.state.next_due_at = time.time() - 1
    loops.save_loop("sid-gateway-loop", mgr.state)
    assert loops.LoopManager(session_id="sid-gateway-loop").fire_tick() is not None

    await GatewayRunner._post_turn_loop_completion(
        runner, session_entry=_FakeSessionEntry(), source=None,
        final_response="Unrelated answer.\nLOOP_COMPLETE", loop_tick=None,
    )
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.status == "active" and reloaded.awaiting_response is True


@pytest.mark.asyncio
async def test_unstamped_turn_releases_a_tick_claimed_before_this_gateway_started(loop_env):
    runner = _make_runner()
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))
    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    assert mgr.state is not None
    mgr.state.next_due_at = time.time() - 1
    loops.save_loop("sid-gateway-loop", mgr.state)
    assert loops.LoopManager(session_id="sid-gateway-loop").fire_tick() is not None
    runner._gateway_started_at = time.time() + 1

    await GatewayRunner._post_turn_loop_completion(
        runner, session_entry=_FakeSessionEntry(), source=None,
        final_response="still polling", loop_tick=None,
    )
    assert loops.load_loop("sid-gateway-loop").awaiting_response is False


def test_slash_command_wake_is_fenced_after_its_tick_completes_but_not_after_a_pause(loop_env):
    from hermes_cli.session_controls import loop_wake_is_current, loop_wake_metadata

    runner = _make_runner()
    del runner
    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    mgr.set("/recap", interval_seconds=300, route={"platform": "telegram", "chat_id": "1"})
    mgr.state.next_due_at = time.time() - 1
    loops.save_loop("sid-gateway-loop", mgr.state)
    fired = loops.LoopManager(session_id="sid-gateway-loop")
    assert fired.fire_tick() == "/recap"
    metadata = loop_wake_metadata("sid-gateway-loop", fired.state, command=True)
    fired.complete_tick("")  # the scheduler completes command ticks right after dispatch
    assert loop_wake_is_current(metadata, "sid-gateway-loop") is True
    loops.LoopManager(session_id="sid-gateway-loop").pause("approved in Telegram")
    assert loop_wake_is_current(metadata, "sid-gateway-loop") is False


@pytest.mark.asyncio
async def test_chain_loop_tick_settles_the_wake_a_human_follow_up_queued_behind(loop_env):
    """Loop wake head + human terminal turn: the chain's loop completion settles the wake's tick."""
    runner = _make_runner()
    runner._gateway_started_at = time.time() - 60
    await GatewayRunner._handle_loop_command(runner, _make_event("/loop 5m poll CI"))
    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    assert mgr.state is not None
    mgr.state.next_due_at = time.time() - 1
    loops.save_loop("sid-gateway-loop", mgr.state)
    fired = loops.LoopManager(session_id="sid-gateway-loop")
    assert fired.fire_tick() is not None
    head = _loop_wake_event(fired)
    # The queued human turn is terminal; the chain recorded the wake's loop tick on the head.
    head._post_turn_goal_identity = {"user_turn": True, "goal_instance": None, "loop_tick": None}
    head._post_turn_loop_tick = [fired.state.created_at, fired.state.ticks_fired]
    runner._post_turn_goal_continuation = AsyncMock()
    await GatewayRunner._run_post_turn_hooks(
        runner, agent_result={"final_response": "answered the human"},
        source=head.source, is_internal=True, event=head,
    )
    assert loops.load_loop("sid-gateway-loop").awaiting_response is False


@pytest.mark.parametrize("cap", ["times", "max_ticks"])
def test_final_capped_command_tick_still_runs(loop_env, cap):
    from hermes_cli.session_controls import loop_wake_is_current, loop_wake_metadata

    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    kwargs = {"times": 1} if cap == "times" else {}
    mgr.set("/recap", interval_seconds=300, route={"platform": "telegram", "chat_id": "1"}, **kwargs)
    if cap == "max_ticks":
        mgr.state.max_ticks = 1
    mgr.state.next_due_at = time.time() - 1
    loops.save_loop("sid-gateway-loop", mgr.state)
    fired = loops.LoopManager(session_id="sid-gateway-loop")
    assert fired.fire_tick() == "/recap"
    metadata = loop_wake_metadata("sid-gateway-loop", fired.state, command=True)
    fired.complete_tick("")
    assert loops.load_loop("sid-gateway-loop").status == ("done" if cap == "times" else "paused")
    assert loop_wake_is_current(metadata, "sid-gateway-loop") is True


@pytest.mark.asyncio
async def test_unaccepted_loop_wake_skips_started_tick(loop_env):
    """A refused wake stays started until the watcher recovers it, never replaying the tick."""
    runner = _make_runner()
    route = {"platform": "discord", "chat_id": "chat-loop", "chat_type": "channel", "user_id": "u"}
    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    mgr.set("poll CI", interval_seconds=300, route=route)
    mgr.state.next_due_at = 0
    loops.save_loop("sid-gateway-loop", mgr.state)

    class _Refusing:
        async def handle_message(self, event):
            event._gateway_accepted = False  # e.g. an occupied internal slot

    adapter = _Refusing()
    runner._adapters_for_profile = lambda profile: {Platform.DISCORD: adapter}
    runner._build_process_event_source = lambda evt: SessionSource(
        platform=Platform.DISCORD, chat_id=evt["chat_id"], chat_type=evt["chat_type"],
        user_id=evt["user_id"])
    runner._session_key_for_source = lambda source: "agent:main:discord:channel:chat-loop"
    runner._running_agents = {}

    async def _inline(func, *args):
        return func(*args)

    runner._run_in_executor_with_context = _inline
    state = loops.load_loop("sid-gateway-loop")
    await GatewayRunner._loop_wakeup_fire_one(runner, "sid-gateway-loop", state, 1e12, set())
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.awaiting_response is True and reloaded.ticks_fired == 1
    await GatewayRunner._loop_wakeup_fire_one(
        runner, "sid-gateway-loop", loops.load_loop("sid-gateway-loop"), 1e12, set())
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.awaiting_response is False and reloaded.ticks_fired == 1


@pytest.mark.asyncio
async def test_direct_adapter_without_receipt_is_a_refusal_not_a_replay(loop_env):
    """Direct adapters follow admit_internal_event's receipt rule: MessageEvent defaults the
    receipt to False, so a handler that never confirms admission leaves the started command tick
    for skip-only recovery. The next scan skips it once and never runs the command again."""
    runner = _make_runner()
    route = {"platform": "discord", "chat_id": "chat-loop", "chat_type": "channel", "user_id": "u"}
    mgr = loops.LoopManager(session_id="sid-gateway-loop")
    mgr.set("/status", interval_seconds=300, route=route)
    mgr.state.next_due_at = 0
    loops.save_loop("sid-gateway-loop", mgr.state)

    class _NoReceipt:
        handled = []

        async def handle_message(self, event):
            self.handled.append(event.text)  # never sets _gateway_accepted

    adapter = _NoReceipt()
    runner._adapters_for_profile = lambda profile: {Platform.DISCORD: adapter}
    runner._build_process_event_source = lambda evt: SessionSource(
        platform=Platform.DISCORD, chat_id=evt["chat_id"], chat_type=evt["chat_type"],
        user_id=evt["user_id"])
    runner._session_key_for_source = lambda source: "agent:main:discord:channel:chat-loop"
    runner._running_agents = {}

    async def _inline(func, *args):
        return func(*args)

    runner._run_in_executor_with_context = _inline
    await GatewayRunner._loop_wakeup_fire_one(
        runner, "sid-gateway-loop", loops.load_loop("sid-gateway-loop"), 1e12, set())
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.awaiting_response is True and reloaded.ticks_fired == 1
    await GatewayRunner._loop_wakeup_fire_one(
        runner, "sid-gateway-loop", loops.load_loop("sid-gateway-loop"), 1e12, set())
    reloaded = loops.load_loop("sid-gateway-loop")
    assert reloaded.awaiting_response is False and reloaded.ticks_fired == 1
    assert adapter.handled == ["/status"]
