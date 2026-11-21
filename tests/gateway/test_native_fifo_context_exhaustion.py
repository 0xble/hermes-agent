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
<<<<<<< HEAD
from gateway.session_authority import SessionAuthority
from tests.fakes.runtime_owner import initialize_session_authority
||||||| dff103e05878
from gateway.session_authority import SessionAuthority, initialize_session_authority
from gateway.session_contract import SessionRef
=======
from gateway.session_authority import SessionAuthority, admission_row, initialize_session_authority
from gateway.session_contract import SessionRef
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
from gateway.session_ingress_context import native_callback
from hermes_constants import get_hermes_home
from plugins.platforms.discord.adapter import DiscordAdapter
from persistence_assembly import close_stores_under

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
<<<<<<< HEAD
        if runner._executor:
            runner._executor.shutdown(wait=True)
        authority.db.close()
||||||| dff103e05878
        authority.db.close()
=======
        close_stores_under(authority.persistence.scope.profile_dir)
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca


async def _admit(native, text, event=None):
    event = event or MessageEvent(text=text, source=native.source, message_id=text)
    with native_callback(native.runner, event, get_hermes_home()):
        return event, await native.authority.admit_native(event)


async def _exhaust(runner, entry, session_key, source):
    from gateway.session_results import execution_result
<<<<<<< HEAD
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
||||||| dff103e05878
    runner, authority, db = native.runner, native.authority, native.authority.db
=======
    runner, authority, db = native.runner, native.authority, native.authority.persistence
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
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
<<<<<<< HEAD
    await authority._drain(original)
    assert executed == [(text, original.session_id) for text in ("A", "B", "C", "D")]
    rows = list_session_admissions(db, session_id=original.session_id, pending_only=False)
    assert [(r["request_id"], r["status"], r["outcome"]) for r in rows] == [
        ("A", "terminal", "failed"), *[(t, "terminal", "completed") for t in ("B", "C", "D")]]
    assert [r["admission_id"] for r in rows[:3]] == [item[1].admission_id for item in admitted]
    _, retry = await _admit(native, "B", admitted[1][0])
||||||| dff103e05878
    await authority._drain(old)
    new = fresh["sid"]
    assert new != old.session_id
    if new in authority.sessions:
        await authority._drain(SessionRef(authority.profile_id, new))
    assert executed == [("A", old.session_id), ("B", new), ("C", new), ("D", new)]
    old_rows = list_session_admissions(db, session_id=old.session_id, pending_only=False)
    assert [(r["request_id"], r["status"]) for r in old_rows] == [("A", "terminal")]
    new_rows = list_session_admissions(db, session_id=new, pending_only=False)
    assert [(r["request_id"], r["status"], r["outcome"]) for r in new_rows] == [
        (t, "terminal", "completed") for t in ("B", "C", "D")]
    # The re-home keeps each message's admission identity; a transport retry of B dedupes onto it.
    assert [r["admission_id"] for r in new_rows[:2]] == [admitted[1][1].admission_id, admitted[2][1].admission_id]
    _, retry = await _admit(native, "B", admitted[1][0])  # the same delivery, redelivered
=======
    await authority._drain(old)
    new = fresh["sid"]
    assert new != old.session_id
    if new in authority.sessions:
        await authority._drain(SessionRef(authority.profile_id, new))
    assert executed == [("A", old.session_id), ("B", new), ("C", new), ("D", new)]
    old_rows = [admission_row(record) for record in db.admissions.list(session_id=old.session_id, pending_only=False)]
    assert [(r["request_id"], r["status"]) for r in old_rows] == [("A", "terminal")]
    new_rows = [admission_row(record) for record in db.admissions.list(session_id=new, pending_only=False)]
    assert [(r["request_id"], r["status"], r["outcome"]) for r in new_rows] == [
        (t, "terminal", "completed") for t in ("B", "C", "D")]
    # The re-home keeps each message's admission identity; a transport retry of B dedupes onto it.
    assert [r["admission_id"] for r in new_rows[:2]] == [admitted[1][1].admission_id, admitted[2][1].admission_id]
    _, retry = await _admit(native, "B", admitted[1][0])  # the same delivery, redelivered
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    assert retry.admission_id == admitted[1][1].admission_id
    assert [t for t, _ in executed].count("B") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("redeliver", [True, False])
<<<<<<< HEAD
async def test_exhaustion_keeps_owner_and_scoped_state_during_redelivery(native, monkeypatch, redeliver):
    runner, authority, db = native.runner, native.authority, native.authority.db
||||||| dff103e05878
async def test_exhaustion_reset_is_ordered_with_redelivery_and_cleanup(native, monkeypatch, redeliver):
    """A redelivery of queued B that resolves the route to the fresh session mid-reset dedupes onto
    the moved row (no UNIQUE collision rolling the whole move back); and the reset schedules the
    moved rows only after the conversation cleanup and topic binding sync."""
    from gateway.session_results import execution_result
    runner, authority, db = native.runner, native.authority, native.authority.db
    loop = asyncio.get_running_loop()
=======
async def test_exhaustion_reset_is_ordered_with_redelivery_and_cleanup(native, monkeypatch, redeliver):
    """A redelivery of queued B that resolves the route to the fresh session mid-reset dedupes onto
    the moved row (no UNIQUE collision rolling the whole move back); and the reset schedules the
    moved rows only after the conversation cleanup and topic binding sync."""
    from gateway.session_results import execution_result
    runner, authority, db = native.runner, native.authority, native.authority.persistence
    loop = asyncio.get_running_loop()
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    admitted = [await _admit(native, text) for text in ("A", "B", "C")]
<<<<<<< HEAD
    original = admitted[0][1].ref
    reset, clear, sync = Mock(), Mock(), Mock()
    monkeypatch.setattr(runner.session_store, "reset_session", reset)
    monkeypatch.setattr(runner, "_clear_conversation_scope", clear)
    monkeypatch.setattr(runner, "_clear_conversation_scope_locked_async", clear)
    monkeypatch.setattr(runner, "_sync_telegram_topic_binding", sync)
    executed = []
||||||| dff103e05878
    old = admitted[0][1].ref
    events, redelivery = [], {}
    real_reset = runner.session_store.reset_session

    def reset_then_redeliver(session_key, *args, **kwargs):
        entry = real_reset(session_key, *args, **kwargs)
        if redeliver:  # the transport redelivers B right after the route rotated
            redelivery["B"] = future = asyncio.run_coroutine_threadsafe(_admit(native, "B", admitted[1][0]), loop)
            try:
                future.result(timeout=1)  # times out when (correctly) ordered behind the move
            except TimeoutError:
                pass
        return entry

    monkeypatch.setattr(runner.session_store, "reset_session", reset_then_redeliver)
    monkeypatch.setattr(runner, "_clear_conversation_scope", lambda *a, **k: events.append("clear"))
    monkeypatch.setattr(runner, "_sync_telegram_topic_binding", lambda *a, **k: events.append("sync"))
    monkeypatch.setattr(SessionAuthority, "_schedule", lambda self, ref: events.append("schedule"))
    executed, fresh = [], {}

    async def turn(event, source, session_key, run_generation):
        entry = runner.session_store.lookup_by_session_key(session_key)
        executed.append(event.text)
        captured = execution_result.get()
        if event.text == "A":
            result = {"failed": True, "compression_exhausted": True, "messages": []}
            captured["result"] = result
            _, new_entry = await runner._hmwa_compression_exhaustion_reset(
                result, "context exhausted", entry, session_key, source)
            fresh["sid"] = new_entry.session_id
        else:
            captured["result"] = {"final_response": "reply", "messages": []}
        return "reply"

    monkeypatch.setattr(runner, "_handle_message_with_agent", turn)
    await authority._drain(old)
    new = fresh["sid"]
    if redeliver:
        _, retry = await asyncio.wrap_future(redelivery["B"])
        assert retry.admission_id == admitted[1][1].admission_id
    else:
        assert events == ["clear", "sync", "schedule"]
    await authority._drain(SessionRef(authority.profile_id, new))
    assert executed == ["A", "B", "C"]
    new_rows = list_session_admissions(db, session_id=new, pending_only=False)
    assert [(r["request_id"], r["admission_id"], r["status"]) for r in new_rows] == [
        ("B", admitted[1][1].admission_id, "terminal"), ("C", admitted[2][1].admission_id, "terminal")]


@pytest.mark.asyncio
async def test_no_scheduler_starts_the_fresh_fifo_before_reset_cleanup_finishes(native, monkeypatch):
    """With the real scheduler, a redelivery of B while the topic binding sync is still running
    (it schedules the fresh FIFO itself) must not start B there before the reset finishes."""
    import threading
    from gateway.session_results import execution_result
    runner, authority = native.runner, native.authority
    loop = asyncio.get_running_loop()
    admitted = [await _admit(native, text) for text in ("A", "B", "C")]
    old = admitted[0][1].ref
    events, executed, entered, release = [], [], asyncio.Event(), threading.Event()

    def held_sync(*args, **kwargs):
        events.append("sync:entered")
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        events.append("sync:done")

    monkeypatch.setattr(runner, "_sync_telegram_topic_binding", held_sync)
=======
    old = admitted[0][1].ref
    events, redelivery = [], {}
    real_reset = runner.session_store.reset_session

    def reset_then_redeliver(session_key, *args, **kwargs):
        entry = real_reset(session_key, *args, **kwargs)
        if redeliver:  # the transport redelivers B right after the route rotated
            redelivery["B"] = future = asyncio.run_coroutine_threadsafe(_admit(native, "B", admitted[1][0]), loop)
            try:
                future.result(timeout=1)  # times out when (correctly) ordered behind the move
            except TimeoutError:
                pass
        return entry

    monkeypatch.setattr(runner.session_store, "reset_session", reset_then_redeliver)
    monkeypatch.setattr(runner, "_clear_conversation_scope", lambda *a, **k: events.append("clear"))
    monkeypatch.setattr(runner, "_sync_telegram_topic_binding", lambda *a, **k: events.append("sync"))
    monkeypatch.setattr(SessionAuthority, "_schedule", lambda self, ref: events.append("schedule"))
    executed, fresh = [], {}

    async def turn(event, source, session_key, run_generation):
        entry = runner.session_store.lookup_by_session_key(session_key)
        executed.append(event.text)
        captured = execution_result.get()
        if event.text == "A":
            result = {"failed": True, "compression_exhausted": True, "messages": []}
            captured["result"] = result
            _, new_entry = await runner._hmwa_compression_exhaustion_reset(
                result, "context exhausted", entry, session_key, source)
            fresh["sid"] = new_entry.session_id
        else:
            captured["result"] = {"final_response": "reply", "messages": []}
        return "reply"

    monkeypatch.setattr(runner, "_handle_message_with_agent", turn)
    await authority._drain(old)
    new = fresh["sid"]
    if redeliver:
        _, retry = await asyncio.wrap_future(redelivery["B"])
        assert retry.admission_id == admitted[1][1].admission_id
    else:
        assert events == ["clear", "sync", "schedule"]
    await authority._drain(SessionRef(authority.profile_id, new))
    assert executed == ["A", "B", "C"]
    new_rows = [admission_row(record) for record in db.admissions.list(session_id=new, pending_only=False)]
    assert [(r["request_id"], r["admission_id"], r["status"]) for r in new_rows] == [
        ("B", admitted[1][1].admission_id, "terminal"), ("C", admitted[2][1].admission_id, "terminal")]


@pytest.mark.asyncio
async def test_no_scheduler_starts_the_fresh_fifo_before_reset_cleanup_finishes(native, monkeypatch):
    """With the real scheduler, a redelivery of B while the topic binding sync is still running
    (it schedules the fresh FIFO itself) must not start B there before the reset finishes."""
    import threading
    from gateway.session_results import execution_result
    runner, authority = native.runner, native.authority
    loop = asyncio.get_running_loop()
    admitted = [await _admit(native, text) for text in ("A", "B", "C")]
    old = admitted[0][1].ref
    events, executed, entered, release = [], [], asyncio.Event(), threading.Event()

    def held_sync(*args, **kwargs):
        events.append("sync:entered")
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        events.append("sync:done")

    monkeypatch.setattr(runner, "_sync_telegram_topic_binding", held_sync)
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

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
<<<<<<< HEAD
    runner, authority, db = native.runner, native.authority, native.authority.db
||||||| dff103e05878
    from gateway.session_results import execution_result
    runner, authority, db = native.runner, native.authority, native.authority.db
=======
    from gateway.session_results import execution_result
    runner, authority, db = native.runner, native.authority, native.authority.persistence
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
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
<<<<<<< HEAD
    assert retry.ref.session_id == late_d.ref.session_id == original.session_id
    await authority._drain(original)
    assert executed == [(text, original.session_id) for text in ("A", "B", "C", "D")]
    assert [r["request_id"] for r in list_session_admissions(db, session_id=original.session_id, pending_only=False)] == [
        "A", "B", "C", "D"]
||||||| dff103e05878
    assert retry.ref.session_id == late_d.ref.session_id == new
    await authority._drain(SessionRef(authority.profile_id, new))
    assert executed == [("A", old.session_id), ("B", new), ("C", new), ("D", new)]
    assert [r["request_id"] for r in list_session_admissions(db, session_id=old.session_id, pending_only=False)] == ["A"]
=======
    assert retry.ref.session_id == late_d.ref.session_id == new
    await authority._drain(SessionRef(authority.profile_id, new))
    assert executed == [("A", old.session_id), ("B", new), ("C", new), ("D", new)]
    assert [r["request_id"] for r in [admission_row(record) for record in db.admissions.list(session_id=old.session_id, pending_only=False)]] == ["A"]
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca


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
