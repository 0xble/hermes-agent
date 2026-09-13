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
        # Failed normal sends recover through the durable-ledger test below.
        # Leave this receipt pending until the successful final-send boundary.
        pass
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


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted,depth_limited", [(True, False), (False, False), (True, True)])
async def test_recursive_delivery_owns_only_its_goal_decision(monkeypatch, tmp_path, interrupted, depth_limited):
    """A discarded predecessor cannot borrow its successor's generation receipt."""
    import socket
    import sys
    from tests.gateway.test_goal_exactly_once import _NoopAgent
    from hermes_cli.goal_outcomes import consume_goal_decision

    network_attempts = []

    def forbid(*args, **kwargs):
        network_attempts.append(args)
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    runner = _setup_runner(monkeypatch, tmp_path)
    adapter = _receipt_adapter()
    adapter._pending_messages = {}
    # Keep agent/transport boundaries fake; run real registration, recursive drain,
    # queued receipt and adapter receipt dispatch with the same generation.
    old = {"final_response": "predecessor", "messages": [], "completed": not interrupted,
           "interrupted": interrupted, "pending_steer": "queued user",
           "_goal_decision": {"should_continue": False}}
    new = {"final_response": "successor", "messages": [], "completed": True,
           "_goal_decision": {"should_continue": False}}
    answers = iter([old, new])

    class Agent(_NoopAgent):
        def run_conversation(self, *args, **kwargs):
            return next(answers)

    monkeypatch.setattr(sys.modules["run_agent"], "AIAgent", Agent)
    cleanup = []

    def schedule_cleanup(response, unused, ctx):
        ctx._post_delivery_adapter = adapter
        adapter.register_post_delivery_callback(
            ctx.session_key, lambda: cleanup.append(response["final_response"]), generation=ctx.run_generation)

    monkeypatch.setattr(runner, "_run_agent_schedule_bubble_cleanup", schedule_cleanup)
    delivered = []

    async def send_first(text, **kwargs):
        delivered.append(text)
        return True

    monkeypatch.setattr(runner, "_deliver_queued_first_response", send_first)
    evaluated = []

    async def consume(**kwargs):
        evaluated.append(kwargs["agent_result"]["final_response"])
        consume_goal_decision(kwargs["agent_result"])

    runner._post_turn_goal_continuation = consume
    states = []
    scheduled_results = []
    schedule = runner._schedule_goal_after_delivery

    def capture(**kwargs):
        states.append(kwargs["state"])
        scheduled_results.append(kwargs["agent_result"])
        schedule(**kwargs)

    runner._schedule_goal_after_delivery = capture
    # Depth-capped interrupted results are returned for outer delivery, not discarded.
    if depth_limited:
        monkeypatch.setattr(runner, "_MAX_INTERRUPT_DEPTH", 0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm")
    state = {}
    result = await runner._run_agent(
        message="first", context_prompt="", history=[], source=source,
        session_id="goal-session", session_key="route", run_generation=7,
        goal_session_entry=SimpleNamespace(session_id="goal-session"), goal_post_turn_state=state,
    )
    delivered.append(result["final_response"])
    callback = adapter._post_delivery_callbacks_by_generation[("route", 7)]
    await adapter._fire_post_delivery_callback("route", asyncio.Event(), 7)
    await callback()  # duplicate receipt must not consume any decision again
    expected = ["predecessor"] if depth_limited else (["successor"] if interrupted else ["predecessor", "successor"])
    assert delivered == expected
    assert evaluated == expected
    assert bool(scheduled_results[0].get("_goal_decision_consumed")) == (not interrupted or depth_limited)
    assert bool(states[0].get("handled")) == (not interrupted or depth_limited)
    assert state["delivery"]["handled"]
    assert set(cleanup) == ({"predecessor"} if depth_limited else {"predecessor", "successor"})
    assert not network_attempts


@pytest.mark.asyncio
@pytest.mark.parametrize('stale', ['current', 'replaced', 'discarded'])
async def test_failed_final_recovers_prepared_goal_via_ledger(receipt_context, monkeypatch, stale):
    from gateway import delivery_ledger as ledger
    from gateway.platforms.base import SendResult

    runner, adapter, mgr, judge = receipt_context
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='12345', chat_type='dm')
    result = {'final_response': 'Partial progress.',
              '_goal_decision': mgr.evaluate_after_turn('Partial progress.')}
    assert result['_goal_decision']['should_continue']
    state = {}
    runner._schedule_goal_after_delivery(adapter=adapter, session_key='route', generation=7,
        session_entry=SimpleNamespace(session_id=mgr.session_id), source=source,
        agent_result=result, state=state)
    cleanup = []
    adapter.register_post_delivery_callback('route', lambda: cleanup.append('clean'), generation=7)
    adapter.typed_command_prefix = '!'
    adapter.gateway_runner = runner
    monkeypatch.setattr(ledger, 'ledger_enabled', lambda: True)
    event = MessageEvent(text='Work on goal', source=source, message_id='receipt-input')
    event._goal_post_turn_state = {'delivery': state}
    oid = await adapter._record_delivery_obligation(event, 'route', result['final_response'], adapter, False)
    assert oid
    await adapter._finalize_delivery_obligation(oid, SendResult(success=False, error='offline'), event, adapter)
    if stale == 'discarded':
        state['discarded'] = True
    await adapter._fire_post_delivery_callback('route', asyncio.Event(), 7, delivery_succeeded=False)
    assert cleanup == ['clean']
    assert not result.get('_goal_decision_consumed') and not runner._overflow_queue('route')
    assert not adapter._post_delivery_callbacks_by_generation
    if stale == 'replaced':
        mgr.set('A replacement goal', max_turns=10)
    # Recover through the real SQLite sweep and sender, without re-registering
    # callbacks or retaining the original event/decision state.
    monkeypatch.setattr(ledger, '_owner_alive', lambda *_: False)
    claimed = ledger.sweep_recoverable()
    assert len(claimed) == 1
    runner._obligation_adapter = AsyncMock(return_value=adapter)
    runner._arm_flood_timers_for_waiting_rows = AsyncMock()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id='delivered'))
    adapter._pending_messages['route'] = MessageEvent(text='User next', source=source)
    assert await runner._redeliver_claimed_obligations(claimed) == 1
    assert len(runner._overflow_queue('route') or []) == (1 if stale == 'current' else 0)
    assert judge.call_count == 1
    # The exact obligation cannot replenish consumed authority by re-recording.
    assert await adapter._record_delivery_obligation(event, 'route', result['final_response'], adapter, False) == oid
    assert ledger.claim_delivered_goal_receipt(oid) is None
    await runner._consume_delivered_goal_receipt(oid)
    assert len(runner._overflow_queue('route') or []) == (1 if stale == 'current' else 0)
    assert ledger.sweep_recoverable() == []


@pytest.mark.asyncio
async def test_normal_durable_receipt_consumes_in_originating_profile(receipt_context, monkeypatch, tmp_path):
    from gateway import delivery_ledger as ledger
    from gateway import run as gateway_run
    from gateway.platforms.base import SendResult
    from hermes_cli import goals
    from hermes_constants import get_hermes_home

    runner, adapter, ambient_manager, judge = receipt_context
    profile_home = tmp_path / 'routed-profile'
    profile_home.mkdir()
    runner.config.multiplex_profiles = True
    runner._resolve_profile_home_for_source = lambda _source: profile_home
    monkeypatch.setattr(gateway_run, '_load_profile_secret_scope', lambda _home: {})
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='12345', chat_type='dm')
    with runner._profile_scope_for_source(source):
        mgr = goals.GoalManager(ambient_manager.session_id)
        mgr.set('Finish the routed profile task', max_turns=10)
        result = {'final_response': 'Profile progress.',
                  '_goal_decision': mgr.evaluate_after_turn('Profile progress.')}
    state = {}
    runner._schedule_goal_after_delivery(adapter=adapter, session_key='route', generation=7,
        session_entry=SimpleNamespace(session_id=mgr.session_id), source=source,
        agent_result=result, state=state)
    callback = adapter._post_delivery_callbacks_by_generation[('route', 7)]
    adapter.typed_command_prefix = '!'
    adapter.gateway_runner = runner
    monkeypatch.setattr(ledger, 'ledger_enabled', lambda: True)
    event = MessageEvent(text='Work', source=source, message_id='profile-final')
    event._goal_post_turn_state = {'delivery': state}
    oid = await adapter._record_delivery_obligation(event, 'route', result['final_response'], adapter, False)
    assert oid and ledger.claim_delivered_goal_receipt(oid) is None
    observed_homes = []
    enqueue = runner._enqueue_fifo
    def capture(*args, **kwargs):
        observed_homes.append(get_hermes_home())
        return enqueue(*args, **kwargs)
    runner._enqueue_fifo = capture
    adapter._pending_messages['route'] = MessageEvent(text='User next', source=source)
    await adapter._finalize_delivery_obligation(oid, SendResult(success=True, message_id='sent'), event, adapter)
    await adapter._fire_post_delivery_callback('route', asyncio.Event(), 7)
    assert state['handled'] and result['_goal_decision_consumed']
    assert observed_homes == [profile_home]
    assert 'routed profile task' in runner._overflow_queue('route')[0].text
    await callback()
    await runner._consume_delivered_goal_receipt(oid)
    assert observed_homes == [profile_home] and judge.call_count == 1
