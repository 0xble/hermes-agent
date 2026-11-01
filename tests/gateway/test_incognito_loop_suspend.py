"""A /loop pauses while its session is private and resumes on the same schedule after off.

A private interval withholds every loop write, so a wakeup would re-inject the same tick on
every scan, and its turn would carry private content into the --until judge.
"""
import pytest

from agent.incognito import register_session
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import loops
from tests.gateway.test_loop_command import _make_runner, loop_env  # noqa: F401  (fixture)

SID, KEY = "sid-gateway-loop", "agent:main:discord:channel:chat-loop"


@pytest.mark.asyncio
async def test_private_loop_wakeup_injects_nothing_and_resumes_after_off(loop_env):  # noqa: F811
    runner = _make_runner()
    route = {"platform": "discord", "chat_id": "chat-loop", "chat_type": "channel", "user_id": "u"}
    mgr = loops.LoopManager(session_id=SID)
    mgr.set("poll CI", interval_seconds=300, route=route)
    mgr.state.next_due_at = 0
    loops.save_loop(SID, mgr.state)
    injected = []

    class _Adapter:
        async def handle_message(self, event):
            injected.append(event)
            event._gateway_accepted = True

    runner._adapters_for_profile = lambda profile: {Platform.DISCORD: _Adapter()}
    runner._build_process_event_source = lambda evt: SessionSource(
        platform=Platform.DISCORD, chat_id=evt["chat_id"], chat_type=evt["chat_type"], user_id=evt["user_id"])
    runner._session_key_for_source = lambda source: KEY
    runner._running_agents = {}

    async def _inline(func, *args):
        return func(*args)

    runner._run_in_executor_with_context = _inline
    before = loops.load_loop(SID)
    register_session(SID, True)
    register_session(KEY, True)
    try:
        for _scan in range(3):
            await GatewayRunner._loop_wakeup_fire_one(runner, SID, loops.load_loop(SID), 1e12, set())
    finally:
        register_session(SID, False)
        register_session(KEY, False)
    paused = loops.load_loop(SID)
    assert injected == []
    assert (paused.ticks_fired, paused.next_due_at, paused.awaiting_response) == (
        before.ticks_fired, before.next_due_at, before.awaiting_response)

    await GatewayRunner._loop_wakeup_fire_one(runner, SID, loops.load_loop(SID), 1e12, set())
    assert len(injected) == 1 and loops.load_loop(SID).ticks_fired == before.ticks_fired + 1


@pytest.mark.asyncio
async def test_cross_session_continuation_for_a_private_target_is_held():
    runner = object.__new__(GatewayRunner)
    record = {"id": "control-1", "target_session_id": "sid-private-target"}
    admitted = []

    async def admit(*_args):
        admitted.append(True)

    runner._mark_and_finish = admit
    register_session("sid-private-target", True)
    try:
        held = await runner._admit_control_continuation(record, None, None, None, "key-private", "continue")
    finally:
        register_session("sid-private-target", False)
    assert held is record and admitted == []
