"""Cross-session continuations and the real privacy switch share admission ownership."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import goals, session_controls
from tests.gateway import test_incognito_turn_e2e as e2e

harness = e2e.harness


@pytest.fixture
def control_topic(harness):
    runner, _store, entry, _config, event, db = harness
    goals._DB_CACHE.clear()
    goals._get_session_db()
    db.create_session("requester", "telegram", profile_name="default")
    manager = goals.GoalManager(entry.session_id)
    manager.set("Finish the public report")
    manager.pause()
    request = session_controls.request_control(
        "goal", "resume", entry.session_id, requester_sid="requester", reason="Continue the report",
    )
    record = session_controls.resolve_request(request["id"], "approve", "synthetic-approver")
    assert record["status"] == "applied" and record.get("continuation_prompt"), record
    adapter = SimpleNamespace(gateway_runner=runner, _active_sessions={}, handled=[])
    runner._get_replay_scheduler().gate.limit = 1
    runner._queue_depth = lambda key, adapter=None: 0

    async def execute(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def accept(incoming):
        # As in native inline dispatch, the callback owns its own model-route lock.
        async with runner._session_model_override_lock(entry.session_key):
            adapter.handled.append(incoming)
            incoming._gateway_accepted = True

    runner._run_in_executor_with_context = execute
    adapter.handle_message = accept

    async def admit():
        return await runner._admit_control_continuation(
            record, session_controls, event("").source, adapter, entry.session_key,
            record["continuation_prompt"],
        )

    yield SimpleNamespace(runner=runner, entry=entry, event=event, adapter=adapter,
                          record=record, admit=admit)
    goals._DB_CACHE.clear()


def hold_control_boundary(topic, monkeypatch, boundary):
    """Pause real validation/settlement; adapter and receipt writes run in physical workers."""
    from gateway.session_runtime_workers import tracked_write

    reached, release, finished = asyncio.Event(), threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def physical(fn, *args, **kwargs):
        loop.call_soon_threadsafe(reached.set)
        try:
            assert release.wait(5), "test did not release the physical operation"
            return fn(*args, **kwargs)
        finally:
            finished.set()

    if boundary == "currency":
        current = session_controls.internal_wake_is_current
        monkeypatch.setattr(session_controls, "internal_wake_is_current",
                            lambda *args: physical(current, *args))
    elif boundary == "pin":
        store = topic.runner.async_session_store
        lookup = store.lookup_by_session_key

        async def pinned(key):
            await asyncio.to_thread(physical, lambda: None)
            return await lookup(key)

        monkeypatch.setattr(store, "lookup_by_session_key", pinned)
    elif boundary == "dispatch":
        accept = topic.adapter.handle_message
        authority = SimpleNamespace()

        async def dispatch(incoming):
            await tracked_write(authority, lambda: physical(lambda: None))
            await accept(incoming)

        topic.adapter.handle_message = dispatch
    else:
        execute = topic.runner._run_in_executor_with_context

        async def executor(fn, *args, **kwargs):
            if fn.__name__ == "mark_outbox":
                # Capacity may have been handed to a turn (or released by an adapter), but
                # neither event ends ownership of the producer's durable outbox receipt.
                gate = topic.runner._get_replay_scheduler().gate
                gate._claimed_since = lambda *_: True
                gate.load()
                return await asyncio.to_thread(physical, fn, *args, **kwargs)
            return await execute(fn, *args, **kwargs)

        monkeypatch.setattr(topic.runner, "_run_in_executor_with_context", executor)
    return reached, release, finished


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["currency", "pin", "dispatch", "settlement"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_real_privacy_switch_waits_for_control_admission_and_physical_settlement(
        control_topic, monkeypatch, boundary, cancelled):
    topic = control_topic
    reached, release, finished = hold_control_boundary(topic, monkeypatch, boundary)
    admission = asyncio.create_task(topic.admit())
    try:
        await asyncio.wait_for(reached.wait(), 2)
        if cancelled:
            admission.cancel()
            await asyncio.sleep(0)
        reply = await asyncio.wait_for(e2e.incognito(topic.runner, topic.event, "/incognito on"), 2)
        assert "not changed" in reply and not topic.entry.incognito, reply
        assert topic.runner._background_admission_pending(topic.entry.session_key)
        assert "OFF" in await e2e.incognito(topic.runner, topic.event, "/incognito status")
        if cancelled:
            assert not admission.done(), "caller abandoned its still-running physical operation"
            admission.cancel()
            await asyncio.sleep(0)
            assert topic.runner._background_admission_pending(topic.entry.session_key)
    finally:
        release.set()
        outcome = await asyncio.wait_for(asyncio.gather(admission, return_exceptions=True), 2)
        assert await asyncio.to_thread(finished.wait, 2)
    if cancelled:
        assert isinstance(outcome[0], asyncio.CancelledError)
    else:
        assert outcome[0]["continuation_enqueued"] is True
    assert session_controls._load_record(topic.record["id"])["continuation_enqueued"] is True
    assert len(topic.adapter.handled) == 1
    assert not topic.runner._background_admission_pending(topic.entry.session_key)
    gate = topic.runner._get_replay_scheduler().gate
    assert gate.load() == (0 if boundary == "settlement" else 1)
    gate._claimed_since = lambda *_: True
    assert gate.load() == 0
    assert "ON" in await e2e.incognito(topic.runner, topic.event, "/incognito on")
    assert "OFF" in await e2e.incognito(topic.runner, topic.event, "/incognito off")


@pytest.mark.asyncio
@pytest.mark.parametrize("deferral", ["private", "transition", "capacity", "refused", "failed", "superseded", "pin"])
async def test_unadmitted_control_releases_capacity_and_keeps_retryable_receipts(
        control_topic, monkeypatch, deferral):
    from gateway.incognito import transition_scope

    topic = control_topic
    runner, key = topic.runner, topic.entry.session_key
    if deferral == "private":
        assert "ON" in await e2e.incognito(runner, topic.event, "/incognito on")
    capacity = gate_token = None
    if deferral == "capacity":
        capacity = runner._get_replay_scheduler().gate
        gate_token = capacity.try_reserve("synthetic-other-session")
        assert gate_token is not None
    with monkeypatch.context() as patch:
        if deferral in {"refused", "failed"}:
            async def refuse(incoming):
                if deferral == "failed":
                    raise RuntimeError("synthetic adapter failure")
            patch.setattr(topic.adapter, "handle_message", refuse)
        if deferral == "superseded":
            goals.GoalManager(topic.entry.session_id).clear()
        if deferral == "pin":
            async def missing(_key):
                return None
            patch.setattr(runner.async_session_store, "lookup_by_session_key", missing)
        if deferral == "transition":
            with transition_scope(key):
                held = await topic.admit()
        else:
            held = await topic.admit()
    if capacity is not None:
        assert capacity.load() == 1 and not capacity.admission_pending(key)
        capacity.release(gate_token)
    assert topic.adapter.handled == []
    persisted = session_controls._load_record(topic.record["id"])
    settled = deferral == "superseded"
    assert held.get("continuation_enqueued", False) is settled
    assert persisted["continuation_enqueued"] is settled
    gate = runner._get_replay_scheduler().gate
    assert not gate.admission_pending(key) and gate.load() == 0
    if settled:
        assert persisted["continuation_discarded"] == "target_changed"
        return
    if deferral == "private":
        assert "OFF" in await e2e.incognito(runner, topic.event, "/incognito off")
    admitted = await topic.admit()
    assert admitted["continuation_enqueued"] is True
    assert session_controls._load_record(topic.record["id"])["continuation_enqueued"] is True
    assert len(topic.adapter.handled) == 1 and gate.load() == 1
    assert not gate.admission_pending(key)
