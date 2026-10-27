"""Turns nobody typed share a gateway-wide cap; typed input never waits behind them.

Regression for the 2026-10-25 Personal gateway flood: after a restart about 120 resumes, goal
continuations, barrier lifts, delegation retries and completions started at once, and a typed
"Hello?" waited 224s for a single 4.9s model call. Wakes are driven through
``gateway.wake.admit_internal_event``, the receipt seam every background wake source uses.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.wake import WakeNotAccepted, admit_internal_event
from tests.waiting import async_wait_until


class _Adapter:
    """Admits like the base adapter and runs each turn until the test ends it."""

    def __init__(self, runner: GatewayRunner):
        self.gateway_runner = runner
        self._active_sessions: dict = {}
        self.admitted: list[str] = []
        self.background: set[str] = set()
        self.peak_background = 0
        self.refusals = 0

    def _event_session_key(self, event) -> str:
        return event.metadata["gateway_session_key"]

    async def handle_message(self, event) -> None:
        key = self._event_session_key(event)
        state = self.gateway_runner._session_state(key)
        # The runner's session claim, as ``_handle_message`` makes it.
        state.turn.agent, state.turn.event = object(), event
        state.turn.started_ts = state.conversation.last_turn_started_at = time.time()
        self.admitted.append(key)
        if event.internal:
            self.background.add(key)
            self.peak_background = max(self.peak_background, len(self.background))
        event._gateway_accepted = True

    def end_turn(self, key: str) -> None:
        self.gateway_runner._session_state(key).turn.clear()
        self.background.discard(key)


def _runner(cap: int) -> tuple[GatewayRunner, _Adapter]:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.config.background_turn_concurrency = cap
    return runner, _Adapter(runner)


def _event(key: str, *, typed: bool = False) -> MessageEvent:
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=key, chat_type="dm", user_id="owner")
    metadata = {"gateway_session_key": key}
    if not typed:
        metadata["notification_origin"] = "process_registry_synthetic"
    return MessageEvent(text="done" if not typed else "Hello?", message_type=MessageType.TEXT,
                        source=source, internal=not typed, metadata=metadata)


async def _settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


async def _admit_with_retry(adapter: _Adapter, event: MessageEvent) -> None:
    """A wake source's retry loop: a refusal leaves its claim in place and it tries again later."""
    while True:
        try:
            return await admit_internal_event(adapter, event)
        except WakeNotAccepted:
            adapter.refusals += 1
            await asyncio.sleep(0.02)  # health: allow HX013 -- stands in for the caller's retry cadence


@pytest.mark.asyncio
async def test_typed_message_starts_ahead_of_a_background_flood_and_the_cap_holds():
    cap, flood = 2, 5
    runner, adapter = _runner(cap)
    wakes = [asyncio.create_task(_admit_with_retry(adapter, _event(f"bg{i}"))) for i in range(flood)]
    await _settle()

    await adapter.handle_message(_event("typed", typed=True))
    assert adapter.admitted.index("typed") <= cap, adapter.admitted
    assert adapter.refusals >= flood - cap  # refused, not queued: no caller sits on a stale claim

    # End each background turn as it starts until every wake has been admitted.
    for expected in range(1, flood + 1):
        await async_wait_until(lambda: len(adapter.admitted) > expected, timeout=10)
        adapter.end_turn(sorted(adapter.background)[0])
    await asyncio.wait_for(asyncio.gather(*wakes), 5)
    # Every deferred wake still fired, and never more than ``cap`` ran at once.
    assert sorted(key for key in adapter.admitted if key != "typed") == [f"bg{i}" for i in range(flood)]
    assert adapter.peak_background <= cap


@pytest.mark.asyncio
async def test_a_wake_for_a_session_mid_turn_joins_it_without_a_slot():
    runner, adapter = _runner(1)
    await admit_internal_event(adapter, _event("other"))  # the only background slot is taken
    await adapter.handle_message(_event("busy", typed=True))

    joined = _event("busy")  # e.g. a completion drained by that turn's own post-turn hooks
    await admit_internal_event(adapter, joined)
    assert joined._gateway_accepted is True


@pytest.mark.asyncio
async def test_a_wake_reserved_before_its_durable_claim_is_never_refused_for_capacity():
    runner, adapter = _runner(1)
    tick = _event("tick")  # a /loop tick reserves, then claims the tick, then admits
    assert runner._reserve_background_turn("tick", tick) is not None
    assert runner._reserve_background_turn("other") is None  # the reservation holds the only slot

    await admit_internal_event(adapter, _event("filler", typed=True))
    runner._session_state("filler").turn.event = _event("filler")  # a background turn took over
    await admit_internal_event(adapter, tick)
    assert tick._gateway_accepted is True


@pytest.mark.asyncio
async def test_a_booting_gateway_admits_no_wake_until_its_startup_restore_finishes():
    runner, adapter = _runner(8)
    runner._startup_restore_in_progress = True  # e.g. coming back from a liveness-watchdog exit
    with pytest.raises(WakeNotAccepted):
        await admit_internal_event(adapter, _event("early"))
    assert runner._reserve_background_turn("tick") is None
    await adapter.handle_message(_event("typed", typed=True))  # typed input is never held

    runner._startup_restore_in_progress = False
    await admit_internal_event(adapter, _event("early"))
    assert adapter.admitted == ["typed", "early"]


def test_queued_restart_resumes_do_not_fill_max_concurrent_sessions():
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner, _adapter = _runner(8)
    runner.config.max_concurrent_sessions = 2
    runner._startup_resume_inflight = {"r1", "r2"}
    for key in ("r1", "r2"):  # preclaimed, still waiting for a background slot
        runner._session_state(key).turn.agent = _AGENT_PENDING_SENTINEL
    assert runner._active_session_limit_message("typed-new-chat") is None

    running = runner._session_state("busy")
    running.turn.agent, running.turn.event = object(), _event("busy", typed=True)
    runner._session_state("busy2").turn.agent = object()
    assert runner._active_session_limit_message("typed-new-chat") is not None
