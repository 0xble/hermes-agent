"""Goal lifecycle fences through real continuation producers and gateway admission."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.gateway.test_session_control_watcher import _runner, state as state


async def _control_event(runner, record):
    captured = []

    async def accept(_adapter, event):
        captured.append(event)
        event._gateway_accepted = True

    with patch("gateway.wake.admit_internal_event", new=AsyncMock(side_effect=accept)):
        await runner._admit_control_continuation(
            record, runner.session_controls, runner.target.origin, runner.adapter,
            runner.target.session_key, record["continuation_prompt"],
        )
    return captured[0]


async def _post_turn_event(runner):
    # Exercise the actual judge/manager and shared constructor, not a hand-built event.
    runner._session_key_for_source = lambda source: runner.target.session_key
    runner._enqueue_fifo = lambda key, event, adapter: adapter._pending_messages.update({key: event})
    runner._goal_max_turns_from_config = lambda: 50
    runner._goal_min_continuation_gap_from_config = lambda: 0
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "work remains", True, None, False)):
        await runner._post_turn_goal_continuation(
            session_entry=runner.target, source=runner.target.origin, final_response="partial progress",
        )
    return runner.adapter._pending_messages[runner.target.session_key]


def _apply(state, action, **payload):
    from hermes_cli import session_controls

    quote = f"Please {action} the target goal immediately"
    state.append_message("requester", "user", quote)
    result = session_controls.apply_control(
        "goal", action, "target", requester_sid="requester", user_quote=quote, payload=payload,
    )
    assert result["ok"], result
    return result["record"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resume", "replace"])
@pytest.mark.parametrize("subsequent", ["pause", "clear", "replace"])
async def test_real_control_constructor_revalidated_at_idle_ingress(state, action, subsequent):
    from gateway.run_inbound import GatewayInboundMixin
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    GoalManager("target").set("old objective")
    GoalManager("target").pause()
    record = _apply(state, action, **({"goal": "approved objective"} if action == "replace" else {}))
    event = await _control_event(runner, record)
    assert event.internal is False  # Production continuations are ordinary authorized ingress.
    if subsequent == "replace":
        GoalManager("target").set("superseding objective")
    else:
        getattr(GoalManager("target"), subsequent)()
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda event, source: event)
    runner._is_user_authorized_for_source = lambda source: True
    runner._admit_bot_message_for_source = lambda source: True
    with patch("gateway.run_inbound._admit_outbox_event", new=AsyncMock()) as admit:
        assert await GatewayInboundMixin._hm_admit_event(runner, event) is None
    admit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "clear"])
async def test_historical_pause_clear_receipt_preserves_fresh_continuation(state, action):
    from gateway.run_busy import GatewayBusySessionMixin
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    GoalManager("target").set("old objective")
    _apply(state, action)
    if action == "clear":
        GoalManager("target").set("new objective")
    else:
        GoalManager("target").resume()
    fresh = await _post_turn_event(runner)
    runner._overflow_queue = lambda key: []
    runner._is_goal_continuation_event = GatewayBusySessionMixin._is_goal_continuation_event
    runner._clear_goal_pending_continuations = lambda key, adapter, before=None: GatewayBusySessionMixin._clear_goal_pending_continuations(runner, key, adapter, before)
    await runner._drain_session_controls()
    assert runner.adapter._pending_messages.get(runner.target.session_key) is fresh


@pytest.mark.asyncio
async def test_replacement_retries_when_only_old_goal_continuation_is_queued(state):
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    GoalManager("target").set("old objective")
    old = await _post_turn_event(runner)
    record = _apply(state, "replace", goal="new objective")
    runner._queue_depth = lambda key, adapter=None: int(key in runner.adapter._pending_messages)
    await runner._drain_session_controls()
    persisted = runner.session_controls._load_record(record["id"])
    assert persisted["continuation_enqueued"] is False
    assert runner.adapter._pending_messages[runner.target.session_key] is old
    runner.adapter._pending_messages.clear()  # FIFO consumer drops the stale item.
    accepted = []

    async def accept(_adapter, event):
        accepted.append(event)
        event._gateway_accepted = True

    with patch("gateway.wake.admit_internal_event", new=AsyncMock(side_effect=accept)):
        await runner._drain_session_controls()
        await runner._drain_session_controls()
    assert len(accepted) == 1
    assert "new objective" in accepted[0].text
    assert runner.session_controls.pending_outbox() == []


@pytest.mark.asyncio
async def test_ordinary_post_turn_continuation_dropped_after_active_replacement(state):
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.run_turn import GatewayTurnMixin
    from gateway.turn_context import TurnContext
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    GoalManager("target").set("old objective")
    old = await _post_turn_event(runner)
    assert "session_control_continuation_id" not in old.metadata
    _apply(state, "replace", goal="new objective")
    runner._MAX_INTERRUPT_DEPTH = 5
    runner._is_goal_continuation_event = GatewayBusySessionMixin._is_goal_continuation_event
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value=None)
    ctx = TurnContext(source=runner.target.origin, session_key=runner.target.session_key,
                      session_id="target", run_generation=1, history=[])
    result = {"interrupted": True, "messages": []}
    returned = await GatewayTurnMixin._run_agent_queued_followup(
        runner, ctx, runner.adapter, old.text, old, "", result, None,
    )
    assert returned is result
    runner._prepare_profile_scoped_inbound_message_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("admission", ["wake", "busy"])
@pytest.mark.parametrize("change", ["replace", "clear_then_set"])
async def test_ordinary_continuation_identity_fences_new_goal_instances(state, admission, change):
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.wake import WakeSuperseded, admit_internal_event
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    GoalManager("target").set("same objective")
    old = await _post_turn_event(runner)
    if change == "replace":
        _apply(state, "replace", goal="same objective")
    else:
        GoalManager("target").clear()
        GoalManager("target").set("same objective")
    assert GoalManager("target").state.created_at != old.metadata["goal_continuation_instance"]
    runner.adapter.handle_message = AsyncMock()
    runner._queue_or_replace_pending_event = AsyncMock()
    if admission == "wake":
        with pytest.raises(WakeSuperseded):
            await admit_internal_event(runner.adapter, old)
        assert old._gateway_accepted is False
    else:
        assert await GatewayBusySessionMixin._handle_active_session_busy_message(
            runner, old, runner.target.session_key,
        ) is True
    runner.adapter.handle_message.assert_not_awaited()
    runner._queue_or_replace_pending_event.assert_not_awaited()


async def _execute_continuation(runner, state, tmp_path, event, path):
    """Run real ingress/FIFO preparation, stopping only at the next model-turn boundary."""
    from types import MethodType
    from gateway.run_turn import GatewayTurnMixin
    from gateway.turn_context import TurnContext
    from gateway.wake import admit_internal_event

    async def accept(current):
        current._gateway_accepted = True

    runner.adapter.handle_message = AsyncMock(side_effect=accept)
    await admit_internal_event(runner.adapter, event)
    runner.adapter.handle_message.assert_awaited_once_with(event)

    if path == "idle":
        from gateway.config import GatewayConfig
        from gateway.session import AsyncSessionStore, SessionStore

        store = SessionStore(tmp_path / "gateway-sessions", GatewayConfig())
        store._db = state
        runner.session_store = store
        runner.async_session_store = AsyncSessionStore(store)
        runner._hmwa_resolve_session = MethodType(GatewayTurnMixin._hmwa_resolve_session, runner)
        runner._recover_telegram_topic_thread_id = lambda source: None
        runner._cache_session_source = lambda key, source: None
        runner._is_telegram_topic_lane = lambda source: False
        runner._PreparedTurn = GatewayTurnMixin._PreparedTurn
        runner._hmwa_prepare_turn = AsyncMock(return_value=(None, None))
        try:
            entry = store.get_or_create_session(event.source)
            runner.target = store.switch_session(entry.session_key, "target")
            runner._session_key_for_source = store._generate_session_key
            if event.metadata.get("gateway_session_strict"):
                event.metadata["gateway_session_key"] = entry.session_key
            await GatewayTurnMixin._handle_message_with_agent(
                runner, event, event.source, entry.session_key, 1,
            )
            runner._hmwa_prepare_turn.assert_awaited_once()
            return runner._hmwa_prepare_turn.await_args.args[0].text
        finally:
            store.close_all_db_handles()

    runner._MAX_INTERRUPT_DEPTH = 5
    runner._session_key_for_source = lambda source: runner.target.session_key
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(
        side_effect=lambda **kw: kw["event"].text,
    )
    runner._reply_anchor_for_event = lambda event: None
    runner._pinned_channel_inputs = lambda key, prompt, source, **kw: (prompt, source)
    runner._persist_prompt_pins = AsyncMock()
    runner._intake_adapter_for = lambda source: None
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._run_agent = AsyncMock(return_value={"final_response": "resumed", "messages": []})
    ctx = TurnContext(source=runner.target.origin, session_key=runner.target.session_key,
                      session_id="target", run_generation=1, history=[])
    await GatewayTurnMixin._run_agent_queued_followup(
        runner, ctx, runner.adapter, event.text, event, "", {"interrupted": True, "messages": []}, None,
    )
    runner._run_agent.assert_awaited_once()
    return runner._run_agent.await_args.kwargs["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["idle", "fifo"])
@pytest.mark.parametrize("change", ["revise", "add_subgoal", "contract_revise", "set_contract"])
async def test_same_goal_definition_edit_refreshes_queued_continuation(state, tmp_path, path, change):
    from hermes_cli.goals import GoalContract, GoalManager
    from hermes_cli.session_controls import _definition_fingerprint, goal_continuation_is_current

    runner = _runner(state)
    instance = GoalManager("target").set("same objective").created_at
    event = await _post_turn_event(runner)
    fingerprint = event.metadata["goal_continuation_fingerprint"]
    criterion = "prove the new acceptance criterion"
    manager = GoalManager("target")
    if change == "add_subgoal":
        manager.add_subgoal(criterion)
    elif change == "set_contract":
        manager.set_contract(GoalContract(verification=criterion))
    else:
        result = manager.revise(
            reason="tighten verification",
            **({"subgoals": [criterion]} if change == "revise" else {"contract": {"verification": criterion}}),
        )
        assert result["ok"], result
    assert manager.state.created_at == instance
    assert criterion not in event.text
    executed = await _execute_continuation(runner, state, tmp_path, event, path)
    assert criterion in executed
    assert executed == GoalManager("target").next_continuation_prompt()
    assert event.text == executed
    assert event.metadata["goal_continuation_instance"] == instance
    assert event.metadata["goal_continuation_fingerprint"] != fingerprint
    assert event.metadata["goal_continuation_fingerprint"] == _definition_fingerprint("goal", manager.state.to_json())
    assert goal_continuation_is_current(event.metadata, "target")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resume", "replace"])
@pytest.mark.parametrize("path", ["idle", "fifo"])
@pytest.mark.parametrize("edit_before_constructor", [False, True])
async def test_approved_control_continuation_refreshes_after_subgoal_add(
    state, tmp_path, action, path, edit_before_constructor,
):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import continuation_is_current, request_control, resolve_request

    runner = _runner(state)
    GoalManager("target").set("same objective")
    GoalManager("target").pause()
    request = request_control("goal", action, "target", requester_sid="requester",
                              payload={"goal": "approved objective"} if action == "replace" else {})
    record = resolve_request(request["id"], "approve", "99")
    assert record["status"] == "applied"
    criterion = "also verify the resumed acceptance criterion"
    if edit_before_constructor:
        GoalManager("target").add_subgoal(criterion)
    event = await _control_event(runner, record)
    if not edit_before_constructor:
        GoalManager("target").add_subgoal(criterion)
    executed = await _execute_continuation(runner, state, tmp_path, event, path)
    assert criterion in executed
    assert continuation_is_current(record["id"])
    assert not runner.session_controls._load_record(record["id"]).get("continuation_discarded")


@pytest.mark.asyncio
async def test_current_continuation_survives_progress_and_resume_but_not_unstamped_ingress(state):
    from gateway.wake import WakeSuperseded, admit_internal_event
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import goal_continuation_is_current

    runner = _runner(state)
    GoalManager("target").set("same objective")
    event = await _post_turn_event(runner)
    manager = GoalManager("target")
    manager.state.turns_used += 1
    manager.state.last_turn_at += 1
    manager.state.consecutive_no_progress += 1
    manager._save()
    assert goal_continuation_is_current(event.metadata, "target")
    assert not goal_continuation_is_current(event.metadata, "requester")
    manager.pause()
    assert not goal_continuation_is_current(event.metadata)
    manager.resume()

    async def accept(current):
        current._gateway_accepted = True

    runner.adapter.handle_message = AsyncMock(side_effect=accept)
    await admit_internal_event(runner.adapter, event)
    runner.adapter.handle_message.assert_awaited_once_with(event)
    event.metadata.pop("goal_continuation_instance")  # Legacy fingerprint-only events fail closed too.
    assert not goal_continuation_is_current(event.metadata, "target")
    with pytest.raises(WakeSuperseded):
        await admit_internal_event(runner.adapter, event)
    event.metadata = {"goal_continuation": True}
    with pytest.raises(WakeSuperseded):
        await admit_internal_event(runner.adapter, event)
    runner.adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_historical_pause_cutoff_removes_old_slot_and_overflow_without_removing_new_work(state):
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.platforms.event import MessageEvent, MessageType
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    GoalManager("target").set("old objective")
    old = await _post_turn_event(runner)
    _apply(state, "pause")
    GoalManager("target").resume()
    fresh = await _post_turn_event(runner)
    human = MessageEvent(text="my queued instruction", message_type=MessageType.TEXT,
                         source=runner.target.origin)
    queue = SimpleNamespace(conversation=SimpleNamespace(queued_events=[old, fresh, human]))
    runner.adapter._pending_messages[runner.target.session_key] = old
    runner._peek_session_state = lambda key: queue
    runner._overflow_queue = lambda key: queue.conversation.queued_events
    runner._is_goal_continuation_event = GatewayBusySessionMixin._is_goal_continuation_event
    runner._clear_goal_pending_continuations = lambda key, adapter, before=None: (
        GatewayBusySessionMixin._clear_goal_pending_continuations(runner, key, adapter, before)
    )
    await runner._drain_session_controls()
    assert runner.target.session_key not in runner.adapter._pending_messages
    assert queue.conversation.queued_events == [fresh, human]


@pytest.mark.asyncio
@pytest.mark.parametrize("admission", ["idle", "busy"])
async def test_current_noninternal_goal_continuation_still_requires_user_authorization(state, admission):
    from unittest.mock import Mock
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.run_inbound import GatewayInboundMixin
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    GoalManager("target").set("current objective")
    event = await _post_turn_event(runner)
    assert event.internal is False
    runner._draining = False
    runner._is_user_authorized_for_source = Mock(return_value=False)
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda event, source: event)
    with patch("gateway.run_inbound._admit_outbox_event", new=AsyncMock()) as admit:
        if admission == "idle":
            assert await GatewayInboundMixin._hm_admit_event(runner, event) is None
        else:
            assert await GatewayBusySessionMixin._handle_active_session_busy_message(
                runner, event, runner.target.session_key,
            ) is True
    runner._is_user_authorized_for_source.assert_called_once_with(event.source)
    admit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("route_change", ["new", "resume", "same"])
@pytest.mark.parametrize("producer", ["post_turn", "control"])
async def test_idle_continuation_cannot_enter_replacement_conversation(
    state, tmp_path, route_change, producer,
):
    from types import MethodType
    from gateway.config import GatewayConfig
    from gateway.run_turn import GatewayTurnMixin
    from gateway.session import AsyncSessionStore, SessionStore
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import goal_continuation_is_current

    runner = _runner(state)
    store = SessionStore(tmp_path / "gateway-sessions", GatewayConfig())
    store._db = state  # Documented pin: route rows and goals share the fixture's SessionDB.
    runner.session_store = store
    runner.async_session_store = AsyncSessionStore(store)
    runner._hmwa_resolve_session = MethodType(GatewayTurnMixin._hmwa_resolve_session, runner)
    runner._recover_telegram_topic_thread_id = lambda source: None
    runner._cache_session_source = lambda key, source: None
    runner._is_telegram_topic_lane = lambda source: False
    runner._PreparedTurn = GatewayTurnMixin._PreparedTurn
    runner._hmwa_prepare_turn = AsyncMock(return_value=(None, None))
    try:
        entry = store.get_or_create_session(runner.target.origin)
        entry = store.switch_session(entry.session_key, "target")
        assert entry is not None
        runner.target = entry
        runner._session_key_for_source = store._generate_session_key
        GoalManager("target").set("old objective still active")
        if producer == "control":
            GoalManager("target").pause()
            event = await _control_event(runner, _apply(state, "resume"))
        else:
            event = await _post_turn_event(runner)
        if route_change == "new":
            replacement = store.reset_session(entry.session_key)
        elif route_change == "resume":
            state.create_session("resumed", source="telegram")
            replacement = store.switch_session(entry.session_key, "resumed")
        else:
            replacement = entry
        assert replacement is not None
        assert (replacement.session_id == "target") == (route_change == "same")
        assert goal_continuation_is_current(event.metadata), "Only routing changed, not the goal"

        await GatewayTurnMixin._handle_message_with_agent(
            runner, event, event.source, entry.session_key, 1,
        )

        if route_change == "same":
            runner._hmwa_prepare_turn.assert_awaited_once()
        else:
            runner._hmwa_prepare_turn.assert_not_awaited()
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_control_continuation_pins_its_destination_session(state):
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    GoalManager("target").set("old objective")
    GoalManager("target").pause()
    event = await _control_event(runner, _apply(state, "resume"))
    assert event.metadata.get("gateway_session_id") == "target"
    assert event.metadata.get("gateway_session_strict") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "clear_then_set"])
async def test_superseded_continuation_turn_is_not_judged_against_the_new_goal(state, change):
    """An old continuation already running when the goal is replaced must not spend the new goal."""
    from hermes_cli.goals import GoalManager, load_goal

    runner = _runner(state)
    GoalManager("target").set("old objective")
    old = await _post_turn_event(runner)
    if change == "replace":
        _apply(state, "replace", goal="new objective", max_turns=1)
    else:
        GoalManager("target").clear()
        GoalManager("target").set("new objective", max_turns=1)
    before = load_goal("target")
    runner.adapter._pending_messages.clear()
    runner.async_session_store = SimpleNamespace(
        get_or_create_session=AsyncMock(return_value=runner.target))
    runner._final_text_for_post_turn_hooks = lambda result, event=None: "finished the old objective"
    runner._is_user_turn_event = lambda event: False
    runner._post_turn_loop_completion = AsyncMock()
    judge = patch("hermes_cli.goals.judge_goal", return_value=("done", "old work done", True, None, False))
    with judge as judged:
        await runner._run_post_turn_hooks(
            agent_result={"final_response": "finished the old objective"},
            source=runner.target.origin, is_internal=True, event=old,
        )
    after = load_goal("target")
    judged.assert_not_called()
    assert after.status == "active" and after.goal == "new objective"
    assert after.turns_used == before.turns_used == 0
    assert runner.target.session_key not in runner.adapter._pending_messages


@pytest.mark.asyncio
async def test_post_turn_judges_the_terminal_queued_turn_not_the_chain_head(state):
    """A current continuation drained behind a superseded head is still judged for its own goal."""
    from hermes_cli.goals import GoalManager, load_goal

    runner = _runner(state)
    GoalManager("target").set("old objective")
    head = await _post_turn_event(runner)
    _apply(state, "replace", goal="new objective")
    runner.adapter._pending_messages.clear()
    terminal = await _post_turn_event(runner)
    runner.adapter._pending_messages.clear()
    head._post_turn_goal_identity = runner._post_turn_goal_identity(terminal)
    runner.async_session_store = SimpleNamespace(
        get_or_create_session=AsyncMock(return_value=runner.target))
    runner._final_text_for_post_turn_hooks = lambda result, event=None: "progress on the new objective"
    runner._post_turn_loop_completion = AsyncMock()
    before = load_goal("target").turns_used
    with patch("hermes_cli.goals.judge_goal",
               return_value=("continue", "work remains", True, None, False)) as judged:
        await runner._run_post_turn_hooks(
            agent_result={"final_response": "progress on the new objective"},
            source=runner.target.origin, is_internal=True, event=head,
        )
    judged.assert_called_once()
    after = load_goal("target")
    assert after.goal == "new objective" and after.turns_used == before + 1
    assert runner.target.session_key in runner.adapter._pending_messages
