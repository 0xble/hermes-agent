"""All non-human origins use the existing wake admission and reservation protocol."""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, turn_origin_for_event
from gateway.run_background_admission import GatewayBackgroundAdmissionMixin, gated_background_kind
from gateway.session import SessionSource
from gateway.wake import WakeNotAccepted, admit_internal_event


class _Runner(GatewayBackgroundAdmissionMixin):
    def __init__(self, limit=1):
        self.config = SimpleNamespace(background_turn_concurrency=limit)
        self._sessions = {}

    def _is_session_running(self, key):
        return key in self._sessions

    def _peek_session_state(self, key):
        return self._sessions.get(key)

    def start(self, key, event):
        self._sessions[key] = SimpleNamespace(
            turn=SimpleNamespace(agent=object(), event=event, started_ts=time.time()),
            conversation=SimpleNamespace(last_turn_started_at=time.time()),
        )

    def end(self, key):
        self._sessions.pop(key)


class _Adapter:
    def __init__(self, runner):
        self.gateway_runner = runner
        self.admitted = []
        self.before_claim: Callable[[], Awaitable[None]] | None = None

    def _event_session_key(self, event):
        return event.metadata["gateway_session_key"]

    async def handle_message(self, event):
        if self.before_claim:
            await self.before_claim()
        key = self._event_session_key(event)
        self.gateway_runner.start(key, event)
        self.admitted.append(key)
        event._gateway_accepted = True


def _event(key, kind):
    source = SessionSource(platform=Platform.LOCAL, chat_id=key)
    metadata = {"gateway_session_key": key}
    internal = False
    if kind == "relay":
        source.delivered_via_upstream_relay = True
    elif kind == "cron":
        metadata["notification_origin"] = "cron_delivery"
    elif kind == "unknown":
        source.is_bot = True
    elif kind == "process":
        metadata["notification_origin"] = "process_registry_synthetic"
        internal = True
    elif kind == "goal_loop":
        metadata["goal_continuation"] = True
        internal = True
    return MessageEvent(text="payload", source=source, metadata=metadata, internal=internal)


@pytest.mark.parametrize("kind", ["relay", "cron", "unknown", "process", "goal_loop"])
def test_gate_uses_the_same_origin_classifier_as_running_turns(kind):
    event = _event("wake", kind)
    assert turn_origin_for_event(event) == kind
    assert gated_background_kind(event) == kind


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["relay", "cron", "unknown"])
async def test_full_gate_refuses_before_claim_and_retry_delivers_each_wake_once(kind):
    runner = _Runner()
    adapter = _Adapter(runner)
    await admit_internal_event(adapter, _event("running", "process"))
    # The source keeps these pending until admission succeeds, as wake producers do.
    pending = {key: _event(key, kind) for key in ("first", "second", "third")}
    for _ in range(2):
        for event in pending.values():
            with pytest.raises(WakeNotAccepted):
                await asyncio.wait_for(admit_internal_event(adapter, event), timeout=1)
            assert not getattr(event, "_gateway_accepted", False)
        assert adapter.admitted == ["running"]
        assert runner._get_replay_scheduler().gate.load() == 1
        assert not runner._get_replay_scheduler().gate._reserved

    # Human input remains admissible at capacity and does not consume background room.
    await admit_internal_event(adapter, _event("human", "user"))
    assert runner._get_replay_scheduler().gate.load() == 1
    previous = "running"
    for key, event in list(pending.items()):
        runner.end(previous)
        await admit_internal_event(adapter, event)
        assert event._gateway_accepted
        pending.pop(key)
        assert runner._get_replay_scheduler().gate.load() == 1
        previous = key
    assert not pending
    assert Counter(adapter.admitted) == Counter(["running", "human", "first", "second", "third"])


@pytest.mark.asyncio
@pytest.mark.parametrize("head_is_human", [False, True])
async def test_merged_genuine_human_input_never_asks_full_gate(head_is_human):
    runner = _Runner()
    adapter = _Adapter(runner)
    await admit_internal_event(adapter, _event("running", "process"))
    human, synthetic = _event("merged", "user"), _event("merged", "cron")
    head, absorbed = (human, synthetic) if head_is_human else (synthetic, human)
    head._hermes_absorbed_events = [absorbed]  # type: ignore[attr-defined]
    assert turn_origin_for_event(head) == "user"
    assert gated_background_kind(head) is None
    await admit_internal_event(adapter, head)
    assert adapter.admitted == ["running", "merged"]
    assert runner._get_replay_scheduler().gate.load() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["relay", "cron", "unknown"])
async def test_reservation_covers_the_await_before_adapter_claim(kind):
    runner = _Runner()
    adapter = _Adapter(runner)
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def before_claim():
        entered.set()
        await proceed.wait()

    adapter.before_claim = before_claim
    first = asyncio.create_task(admit_internal_event(adapter, _event("first", kind)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert not adapter.admitted
        assert runner._get_replay_scheduler().gate.load() == 1
        with pytest.raises(WakeNotAccepted):
            await admit_internal_event(adapter, _event("second", kind))
        proceed.set()
        await asyncio.wait_for(first, timeout=1)
        assert adapter.admitted == ["first"]
        assert runner._get_replay_scheduler().gate.load() == 1
        runner.end("first")
        await admit_internal_event(adapter, _event("second", kind))
        assert adapter.admitted == ["first", "second"]
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["relay", "cron", "unknown"])
async def test_capacity_reservation_is_honored_after_durable_source_claim(kind):
    runner = _Runner()
    adapter = _Adapter(runner)
    event = _event("reserved", kind)
    token = runner._reserve_background_turn("reserved", event)
    assert token is not None
    assert runner._reserve_background_turn("other") is None
    # A caller can safely consume its durable source only after reserving.
    runner.start("filler", _event("filler", "process"))
    await admit_internal_event(adapter, event)
    assert adapter.admitted == ["reserved"]
    assert event._gateway_accepted
    assert runner._get_replay_scheduler().gate.load() == 2
    assert token not in runner._get_replay_scheduler().gate._reserved


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["relay", "cron", "unknown"])
async def test_failed_adapter_claim_releases_room_for_retry(kind):
    runner = _Runner()
    adapter = _Adapter(runner)

    async def fail():
        raise RuntimeError("adapter refused before claim")

    adapter.before_claim = fail
    event = _event("retry", kind)
    with pytest.raises(RuntimeError, match="adapter refused"):
        await admit_internal_event(adapter, event)
    assert not adapter.admitted
    assert runner._get_replay_scheduler().gate.load() == 0
    adapter.before_claim = None
    await admit_internal_event(adapter, event)
    assert adapter.admitted == ["retry"]
