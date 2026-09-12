"""Goal admission belongs to the final-delivery receipt, not agent completion."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import MessageEvent
from gateway.config import Platform
from gateway.session import SessionSource
from tests.gateway.test_goal_exactly_once import _setup_runner, _receipt_adapter


@pytest.mark.asyncio
async def test_agent_completion_without_receipt_cannot_advance_goal(monkeypatch, tmp_path):
    runner = _setup_runner(monkeypatch, tmp_path)
    entry = SimpleNamespace(session_id="goal-no-receipt")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm")
    runner.session_store.get_or_create_session = MagicMock(return_value=entry)
    runner._post_turn_goal_continuation = AsyncMock()
    state = {}
    await runner._run_agent(
        message="continue", context_prompt="", history=[], session_id=entry.session_id,
        source=source, session_key="agent:main:telegram:dm:12345",
        goal_session_entry=entry, goal_post_turn_state=state,
    )
    runner._post_turn_goal_continuation.assert_not_awaited()
    assert not state.get("handled")
    assert not state["delivery"].get("handled")


@pytest.fixture
def receipt_context(monkeypatch, tmp_path):
    from hermes_cli import goals
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    runner = _setup_runner(monkeypatch, tmp_path)
    token = set_hermes_home_override(str(tmp_path / "home"))
    goals._DB_CACHE.clear()
    goals._get_session_db()  # warm on sync fixture thread, not the event loop
    mgr = goals.GoalManager("goal-receipt")
    mgr.set("Finish the local fix", max_turns=10)
    judge = MagicMock(return_value=("continue", "still working", False, None, False))
    monkeypatch.setattr(goals, "judge_goal", judge)
    adapter = _receipt_adapter()
    adapter._pending_messages = {}
    runner._adapter_for_source = lambda _source: adapter
    runner._session_key_for_source = lambda _source: "route"
    runner._send_goal_status_notice = AsyncMock()
    yield runner, adapter, mgr, judge
    goals._DB_CACHE.clear()
    reset_hermes_home_override(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["normal", "queued"])
@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("pending", [False, True])
async def test_receipt_gates_consumption_retry_and_fifo(receipt_context, lane, prepared, pending):
    from hermes_cli.goals import collect_tool_evidence

    runner, adapter, mgr, judge = receipt_context
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm")
    result = {"final_response": "partial", "messages": [
        {"role": "assistant", "tool_calls": [{"id": "canonical", "function": {
            "name": "terminal", "arguments": '{"command":"pytest focused"}'}}]},
        {"role": "tool", "tool_call_id": "canonical", "content": '{"exit_code":0}', "timestamp": time.time()},
    ]}
    if prepared:
        result["_goal_decision"] = mgr.evaluate_after_turn(
            "partial", tool_evidence=collect_tool_evidence(result))
        assert result["_goal_decision"]["should_continue"]
    state = {}
    runner._schedule_goal_after_delivery(
        adapter=adapter, session_key="route", generation=7,
        session_entry=SimpleNamespace(session_id=mgr.session_id), source=source,
        agent_result=result, state=state, same_session_pending=pending,
    )
    assert judge.call_count == int(prepared)
    assert not result.get("_goal_decision_consumed")
    callback = adapter._post_delivery_callbacks_by_generation[("route", 7)]
    # A queued user turn is admitted while this response is awaiting delivery.
    user = MessageEvent(text="user follow-up", source=source)
    adapter._pending_messages["route"] = user
    if lane == "normal":
        await adapter._fire_post_delivery_callback("route", asyncio.Event(), 7, delivery_succeeded=False)
    else:
        # The real queued-delivery owner must not fire its callback on a failed
        # send. Only the transport/final-send boundary is fake.
        runner._deliver_queued_first_response = AsyncMock(side_effect=[False, True])
        ctx = SimpleNamespace(
            source=source, session_key="route", run_generation=7,
            stream_consumer_holder=[None], _status_thread_metadata=None,
            _post_delivery_owner=adapter, inbound_message_id="inbound", event_message_id=None,
        )
        # The drain has already popped the queued event before sending.
        adapter._pending_messages.pop("route")
        assert not await runner._run_agent_deliver_first_response(
            ctx, adapter, result, result, None, user, user.text)
    assert not state.get("handled")
    assert not result.get("_goal_decision_consumed")
    assert judge.call_count == int(prepared)
    assert adapter._pending_messages["route"] is user
    assert not runner._overflow_queue("route")

    if lane == "normal":
        # A retried successful receipt can reuse the unconsumed decision. This
        # does not introduce an automatic retry queue for failed deliveries.
        adapter.register_post_delivery_callback("route", callback, generation=7)
        await adapter._fire_post_delivery_callback("route", asyncio.Event(), 7)
    else:
        assert await runner._run_agent_deliver_first_response(
            ctx, adapter, result, result, None, user, user.text)
    assert state["handled"]
    assert judge.call_count == 1  # preprepared decisions never rejudge
    assert judge.call_args.kwargs["tool_evidence"][0]["tool_call_id"] == "canonical"
    if prepared:
        assert result["_goal_decision_consumed"]
    assert adapter._pending_messages["route"] is user
    queued = runner._overflow_queue("route") or []
    assert len(queued) == (0 if pending else 1)
    if queued:
        assert "Finish the local fix" in queued[0].text
    await callback()  # duplicate receipt cannot claim/queue/evaluate twice
    assert judge.call_count == 1
    assert len(runner._overflow_queue("route") or []) == (0 if pending else 1)
