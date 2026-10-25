"""Retirement decisions stay quiet, recoverable, and visible in the final note."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.telegram_retire import load
from gateway import telegram_retire as retire
from gateway.telegram_retire_prepare import closing_note, queue_preparation
from gateway.telegram_topic_sessions import TopicRequestError
from gateway.wake import WakeNotAccepted
from hermes_state import AsyncSessionDB, SessionDB


@pytest.fixture
def runtime(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="101", thread_id="17", chat_type="dm",
                           message_id="original")
    state = {"generation": "gen", "phase": "pending", "session_id": "old", "session_key": "old-key"}
    async def accept(event):
        event._gateway_accepted = True
    adapter = SimpleNamespace(handle_message=AsyncMock(side_effect=accept), send=AsyncMock())
    runner = SimpleNamespace(_session_db=AsyncSessionDB(db), _delivery_adapter_for=lambda _: adapter)
    yield SimpleNamespace(db=db, runner=runner, source=source, state=state, adapter=adapter)
    db.close()


@pytest.mark.asyncio
async def test_preparation_is_silent_pinned_deduplicated_and_readmitted_after_restart(runtime):
    r = runtime
    items = [{"id": "loop", "kind": "loop", "title": "Watch Service"}]
    assert not await queue_preparation(r.runner, r.source, r.state, items)
    event = r.adapter.handle_message.await_args.args[0]
    assert event.internal and event.reply_expected is False
    assert event.source.message_id is None
    assert event.metadata == {"gateway_session_id": "old", "gateway_session_strict": True,
                              "retirement_generation": "gen"}
    assert "loop" in event.text
    assert not await queue_preparation(r.runner, r.source, r.state, items)
    assert r.adapter.handle_message.await_count == 1
    recovered = await load(r.runner, r.source)
    assert recovered["phase"] == "waiting"
    del r.runner._telegram_retire_preparations
    assert not await queue_preparation(r.runner, r.source, recovered, items)
    assert r.adapter.handle_message.await_count == 2
    r.adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope,field,value", [
    ("item", "id", "other"),
    ("item", "kind", "cron"),
    ("item", "title", "Another Service"),
    ("item", "blocked_owner", "Service Operations"),
    ("item", "blocked_reason", "Destination is retiring"),
    ("state", "reason", "Its lasting subject moved"),
    ("state", "target", "Service Operations"),
    ("state", "context", "Continue the service watch"),
    ("state", "owners", {"loop": "Service Operations"}),
    ("busy", "busy", "Active claim still running"),
])
async def test_preparation_rearms_for_changed_decisions_not_inventory_order(runtime, scope, field, value):
    r = runtime
    items = [{"id": "loop", "kind": "loop", "title": "Watch Service"},
             {"id": "goal", "kind": "goal", "title": "Operate Service"}]
    assert not await queue_preparation(r.runner, r.source, r.state, items)
    assert not await queue_preparation(r.runner, r.source, r.state, list(reversed(items)))
    assert r.adapter.handle_message.await_count == 1
    busy = ""
    if scope == "item":
        items[0][field] = value
    elif scope == "state":
        r.state[field] = value
    else:
        busy = value
    assert not await queue_preparation(r.runner, r.source, r.state, items, busy=busy)
    assert r.adapter.handle_message.await_count == 2
    assert not await queue_preparation(r.runner, r.source, r.state, items, busy=busy)
    assert r.adapter.handle_message.await_count == 2
    r.adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_refused_preparation_can_retry_without_consuming_the_wake(runtime):
    r = runtime
    r.adapter.handle_message.side_effect = None
    with pytest.raises(WakeNotAccepted):
        await queue_preparation(r.runner, r.source, r.state, [])
    assert r.runner._telegram_retire_preparations == {}
    async def accept(event):
        event._gateway_accepted = True
    r.adapter.handle_message.side_effect = accept
    assert not await queue_preparation(r.runner, r.source, r.state, [])
    assert r.adapter.handle_message.await_count == 2
    r.adapter.send.assert_not_awaited()


def test_closing_note_records_choice_reason_and_continuing_destinations():
    note = closing_note({"target": "Service Operations", "reason": "It owns the continuing service."})
    assert note == ("Merged into Service Operations: It owns the continuing service. "
                    "Its session and transcript are preserved.")
    note = closing_note({"reason": "The purchase is complete.", "handoffs": {
        "cron:a": {"moved": True, "destination": {"title": "Delivery Tracking"}},
        "cron:b": {"moved": True, "destination": {"title": "Delivery Tracking"}},
        "loop": {"moved": False, "destination": {"title": "Unconfirmed"}}}})
    assert note == ("Retired: The purchase is complete. Open work continues in Delivery Tracking. "
                    "Its session and transcript are preserved.")


def bind_caller(runtime):
    runtime.runner._session_key_for_source = lambda _: "old-key"
    runtime.runner.session_store = SimpleNamespace(peek_session_id=lambda _: "old")


@pytest.mark.asyncio
async def test_decision_preserves_handoffs_clears_merge_and_cannot_rearm_after_cancellation(runtime):
    r = runtime
    bind_caller(r)
    r.state.update(target="Service Operations", handoffs={"task:a": {"receipt_id": "proof", "moved": True}})
    await retire.save(r.runner, r.source, r.state)
    result = await retire.request(r.runner, r.source, target="", owners={"loop": "Monitoring"},
        reason="The purchase is complete.", generation="gen")
    decided = await load(r.runner, r.source)
    assert decided is not None
    assert decided["target"] == "" and decided["owners"] == {"loop": "Monitoring"}
    assert decided["handoffs"] == r.state["handoffs"] and decided["handoff_generation"] == "gen"
    assert decided["generation"] == result["generation"] != "gen"
    again = await retire.request(r.runner, r.source, generation=decided["generation"])
    assert again["generation"] == decided["generation"]
    await retire.request(r.runner, r.source, cancel=True)
    cancelled = await load(r.runner, r.source)
    with pytest.raises(TopicRequestError, match="superseded"):
        await retire.request(r.runner, r.source, reason="Stale preparation.", generation=decided["generation"])
    assert await load(r.runner, r.source) == cancelled
    r.adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_between_preparation_read_and_save_is_fenced(runtime, monkeypatch):
    r = runtime
    bind_caller(r)
    await retire.save(r.runner, r.source, r.state)
    original = retire.load
    read, resume = asyncio.Event(), asyncio.Event()
    pause_first = True
    async def delayed_load(*args):
        nonlocal pause_first
        current = await original(*args)
        if pause_first:
            pause_first = False
            read.set()
            await resume.wait()
        return current
    monkeypatch.setattr(retire, "load", delayed_load)
    preparing = asyncio.create_task(retire.request(r.runner, r.source, reason="Finished.", generation="gen"))
    await read.wait()
    await retire.request(r.runner, r.source, cancel=True)
    cancelled = await original(r.runner, r.source)
    resume.set()
    with pytest.raises(TopicRequestError, match="superseded"):
        await preparing
    assert await original(r.runner, r.source) == cancelled
    r.adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target,context,missing", [
    ("Service Operations", "", "context"), ("Missing Owner", "Useful context.", "destination"),
])
async def test_incomplete_merge_returns_to_quiet_preparation(runtime, monkeypatch, target, context, missing):
    from gateway import telegram_retire_work as work
    r = runtime
    r.state.update(target=target, context=context, reason="Same subject.", handoffs={})
    monkeypatch.setattr(work, "inventory", AsyncMock(return_value=[]))
    resolve = AsyncMock(side_effect=ValueError("Owner is unavailable"))
    monkeypatch.setattr(work, "destination", resolve)
    assert not await work.prepare(r.runner, r.source, r.state)
    event = r.adapter.handle_message.await_args.args[0]
    assert event.internal and event.metadata["retirement_generation"] == "gen"
    if missing == "context":
        resolve.assert_not_awaited()
    else:
        assert "Owner is unavailable" in event.text
    r.adapter.send.assert_not_awaited()
