"""A gateway stop settles authority work within the watchdog leash it has left, and never closes
state.db under an authority turn that outlived the settle (N59).

The settle used to wait a flat TURN_SETTLE_SECONDS (45 s) whatever the drain and interrupt phases had
already spent, so the stop ran past its watchdog leash; and the SessionDB close gate counted executor,
cron, API and deferred workers but not the authority's still-running turn, so it closed and
checkpointed state.db under a live writer.
"""
import asyncio
import json
import time
from functools import partial
from types import SimpleNamespace

import pytest

import gateway.run as gw_mod
from gateway import run_shutdown
from gateway.platforms.event import MessageEvent
from tests.gateway.restart_test_helpers import make_restart_source
from tests.gateway.test_shutdown_executor_quiesce import _FakeGateway


@pytest.mark.asyncio
async def test_stop_settles_within_the_leash_and_keeps_state_db_open_for_a_live_turn(monkeypatch):
    events = []
    gw = _FakeGateway(events)
    # A 12 s watchdog leash: the settle may spend at most what is left of it minus the tail reserve.
    monkeypatch.setattr(run_shutdown, "resolve_shutdown_watchdog_delay", lambda drain, **_k: 12.0)
    never = asyncio.Event()
    turn = asyncio.create_task(never.wait())  # an executing turn that ignores its Stop
    live = SimpleNamespace(task=turn, route="r", event_stream=SimpleNamespace(execution={"execution_generation": 1}))
    gw.session_authority = SimpleNamespace(
        profile_id="default", sessions={"s": live}, _managed_workers={}, adopted={}, pending_stops={},
        runner=gw)
    started = time.monotonic()
    try:
        async with asyncio.timeout(10):
            await gw_mod.GatewayRunner.stop(gw)
    finally:
        turn.cancel()
    assert time.monotonic() - started < 6, "the settle overran the stop's watchdog leash"
    assert "close:session_db" not in events, f"state.db closed under a live authority turn: {events}"


@pytest.mark.asyncio
async def test_stop_spools_queued_input_before_waiting_for_authority(tmp_path, monkeypatch):
    gw = _FakeGateway([])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.run_pending_recovery.get_routing_process_hermes_home", lambda: tmp_path)
    gw._flush_owned_pending = partial(run_shutdown.GatewayShutdownMixin._flush_owned_pending, gw)
    key = "agent:main:telegram:dm:123456"
    source = make_restart_source()
    first = MessageEvent(text="already queued", source=source, message_id="first")
    gw._pending_messages[key] = first

    def spooled_ids():
        return sorted(json.loads(path.read_text(encoding="utf-8"))["data"]["message_id"]
                      for path in (tmp_path / "pending_messages").glob("*.json"))

    async def settle(runner, *_args):
        assert spooled_ids() == ["first"]
        runner._pending_messages[key] = MessageEvent(text="queued during settle", source=source, message_id="second")

    monkeypatch.setattr("gateway.run_runtime.settle_gateway_runtime", settle)
    await gw_mod.GatewayRunner.stop(gw)
    assert spooled_ids() == ["first", "second"]
    assert gw._pending_messages == {}
