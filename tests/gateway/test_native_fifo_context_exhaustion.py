"""Compression exhaustion retains native FIFO ownership and dedupe, without replaying the failed
turn. Real GatewayRunner, DiscordAdapter, SessionAuthority and drain over SQLite, with a stand-in
turn. Includes delivery and route-reservation races adapted from helix4u's #139673 headline test.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pytest_asyncio

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_authority import SessionAuthority, initialize_session_authority
from gateway.session_ingress_context import native_callback
from hermes_constants import get_hermes_home
from hermes_state_runtime import list_session_admissions
from plugins.platforms.discord.adapter import DiscordAdapter

_REAL_SCHEDULE = SessionAuthority._schedule


@pytest_asyncio.fixture
async def native(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")
    monkeypatch.setattr(SessionAuthority, "_schedule", lambda self, ref: None)
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fixture-token", typing_indicator=False))

    async def send(chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="fixture-reply")

    monkeypatch.setattr(adapter, "send", send)
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="fixture")
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id="42", chat_type="dm", user_id="42")
    try:
        yield SimpleNamespace(runner=runner, adapter=adapter, authority=authority, source=source)
    finally:
        from gateway.session_runtime_workers import mutation_tasks
        await asyncio.gather(*mutation_tasks(authority))
        if runner._executor:
            runner._executor.shutdown(wait=True)
        authority.db.close()


async def _admit(native, text, event=None):
    event = event or MessageEvent(text=text, source=native.source, message_id=text)
    with native_callback(native.runner, event, get_hermes_home()):
        return event, await native.authority.admit_native(event)


async def _exhaust(runner, entry, session_key, source):
    from gateway.session_results import execution_result
    result = {"failed": True, "compression_exhausted": True, "messages": []}
    slot = execution_result.get()
    assert slot is not None
    slot["result"] = result
    response, retained = await runner._hmwa_compression_exhaustion_reset(
        result, "context exhausted", entry, session_key, source)
    assert retained is entry
    assert response.startswith("context exhausted")
    assert not runner.session_authority.deferred_resets


def _completed():
    from gateway.session_results import execution_result
    slot = execution_result.get()
    assert slot is not None
    slot["result"] = {"final_response": "reply", "messages": []}


@pytest.mark.asyncio
async def test_exhaustion_runs_queued_native_messages_on_the_original_session(native, monkeypatch):
    runner, authority, db = native.runner, native.authority, native.authority.db
    admitted = [await _admit(native, text) for text in ("A", "B", "C")]
    original = admitted[0][1].ref
    executed = []

    async def turn(event, source, session_key, run_generation):
        entry = runner.session_store.lookup_by_session_key(session_key)
        executed.append((event.text, entry.session_id))
        if event.text == "A":
            await _exhaust(runner, entry, session_key, source)
            await _admit(native, "D")
        else:
            _completed()
        return "reply"

    monkeypatch.setattr(runner, "_handle_message_with_agent", turn)
    await authority._drain(original)
    assert executed == [(text, original.session_id) for text in ("A", "B", "C", "D")]
    rows = list_session_admissions(db, session_id=original.session_id, pending_only=False)
    assert [(r["request_id"], r["status"], r["outcome"]) for r in rows] == [
        ("A", "terminal", "failed"), *[(t, "terminal", "completed") for t in ("B", "C", "D")]]
    assert [r["admission_id"] for r in rows[:3]] == [item[1].admission_id for item in admitted]
    _, retry = await _admit(native, "B", admitted[1][0])
    assert retry.admission_id == admitted[1][1].admission_id
    assert [t for t, _ in executed].count("B") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("redeliver", [True, False])
async def test_exhaustion_keeps_owner_and_scoped_state_during_redelivery(native, monkeypatch, redeliver):
    runner, authority, db = native.runner, native.authority, native.authority.db
    admitted = [await _admit(native, text) for text in ("A", "B", "C")]
    original = admitted[0][1].ref
    reset, clear, sync = Mock(), Mock(), Mock()
    monkeypatch.setattr(runner.session_store, "reset_session", reset)
    monkeypatch.setattr(runner, "_clear_conversation_scope", clear)
    monkeypatch.setattr(runner, "_clear_conversation_scope_locked_async", clear)
    monkeypatch.setattr(runner, "_sync_telegram_topic_binding", sync)
    executed = []

    async def turn(event, source, session_key, run_generation):
        entry = runner.session_store.lookup_by_session_key(session_key)
        executed.append((event.text, entry.session_id))
        if event.text == "A":
            await _exhaust(runner, entry, session_key, source)
            if redeliver:
                _, retry = await _admit(native, "B", admitted[1][0])
                assert retry.admission_id == admitted[1][1].admission_id
        else:
            _completed()
        return "reply"

    monkeypatch.setattr(runner, "_handle_message_with_agent", turn)
    await authority._drain(original)
    assert executed == [(text, original.session_id) for text in ("A", "B", "C")]
    for forbidden in (reset, clear, sync):
        forbidden.assert_not_called()
    rows = list_session_admissions(db, session_id=original.session_id, pending_only=False)
    assert [(r["request_id"], r["admission_id"], r["status"]) for r in rows] == [
        (text, receipt.admission_id, "terminal") for text, (_, receipt) in zip(("A", "B", "C"), admitted)]


@pytest.mark.asyncio
async def test_redelivery_cannot_start_followers_before_the_exhausted_turn_finishes(native, monkeypatch):
    runner, authority = native.runner, native.authority
    admitted = [await _admit(native, text) for text in ("A", "B", "C")]
    original = admitted[0][1].ref
    executed, entered, release, finished = [], asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def turn(event, source, session_key, run_generation):
        entry = runner.session_store.lookup_by_session_key(session_key)
        executed.append((event.text, entry.session_id))
        if event.text == "A":
            await _exhaust(runner, entry, session_key, source)
            runner._release_running_agent_state(session_key, run_generation=run_generation)
            entered.set()
            await release.wait()
        else:
            assert release.is_set()
            _completed()
            if event.text == "C":
                finished.set()
        return "reply"

    monkeypatch.setattr(runner, "_handle_message_with_agent", turn)
    monkeypatch.setattr(SessionAuthority, "_schedule", _REAL_SCHEDULE)
    authority._schedule(original)
    try:
        await asyncio.wait_for(entered.wait(), 10)
        _, retry = await _admit(native, "B", admitted[1][0])
        assert retry.admission_id == admitted[1][1].admission_id
        assert executed == [("A", original.session_id)]
    finally:
        release.set()
    await asyncio.wait_for(finished.wait(), 10)
    await authority._drain(original)
    assert executed == [(text, original.session_id) for text in ("A", "B", "C")]


@pytest.mark.asyncio
async def test_admission_reserved_before_exhaustion_stays_on_the_original_fifo(native, monkeypatch):
    import threading
    runner, authority, db = native.runner, native.authority, native.authority.db
    loop = asyncio.get_running_loop()
    admitted = [await _admit(native, text) for text in ("A", "B", "C")]
    original = admitted[0][1].ref
    reserve_real, reserved, release = authority._reserve_route, asyncio.Event(), threading.Event()
    held = []

    def held_reserve(src):
        answer = reserve_real(src)
        held.append(answer)
        loop.call_soon_threadsafe(reserved.set)
        assert release.wait(10)
        return answer

    monkeypatch.setattr(authority, "_reserve_route", held_reserve)
    late = [asyncio.create_task(_admit(native, "B", admitted[1][0])), asyncio.create_task(_admit(native, "D"))]
    executed = []

    async def turn(event, source, session_key, run_generation):
        entry = runner.session_store.lookup_by_session_key(session_key)
        executed.append((event.text, entry.session_id))
        if event.text == "A":
            await _exhaust(runner, entry, session_key, source)
        else:
            _completed()
        return "reply"

    monkeypatch.setattr(runner, "_handle_message_with_agent", turn)
    try:
        await asyncio.wait_for(reserved.wait(), 10)
        assert [sid for sid, _ in held] == [original.session_id]
        await authority._drain(original)
    finally:
        release.set()
    (_, retry), (_, late_d) = await asyncio.gather(*late)
    assert retry.admission_id == admitted[1][1].admission_id
    assert retry.ref.session_id == late_d.ref.session_id == original.session_id
    await authority._drain(original)
    assert executed == [(text, original.session_id) for text in ("A", "B", "C", "D")]
    assert [r["request_id"] for r in list_session_admissions(db, session_id=original.session_id, pending_only=False)] == [
        "A", "B", "C", "D"]


@pytest.mark.asyncio
async def test_exhaustion_keeps_the_adapter_queue_on_its_original_session(native):
    runner, adapter, source = native.runner, native.adapter, native.source
    _, receipt = await _admit(native, "A")
    session_key = native.authority.sessions[receipt.ref.session_id].route
    head, second = (MessageEvent(text=t, source=source, message_id=t) for t in ("Q1", "Q2"))
    runner._enqueue_fifo(session_key, head, adapter)
    runner._enqueue_fifo(session_key, second, adapter)
    entry = runner.session_store.lookup_by_session_key(session_key)
    result = {"failed": True, "compression_exhausted": True, "messages": []}
    _, retained = await runner._hmwa_compression_exhaustion_reset(
        result, "context exhausted", entry, session_key, source)
    assert retained is entry
    assert adapter._pending_messages[session_key] is head
    assert runner._overflow_queue(session_key) == [second]
    assert not native.authority.deferred_resets
