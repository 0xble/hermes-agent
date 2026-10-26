"""Goal lifecycle fences through real continuation producers and gateway admission."""
from __future__ import annotations

import uuid
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
    from hermes_cli.goals import GoalManager

    manager = GoalManager(runner.target.session_id)
    if manager.state is not None:
        # This identity suite is not a pacing test; ensure a fresh synthetic goal turn is admitted
        # even when the fixture session was reused after a prior continuation.
        manager.state.last_automatic_continuation_at = 0.0
        manager._save()
    runner._session_key_for_source = lambda source: runner.target.session_key
    runner._enqueue_fifo = lambda key, event, adapter: adapter._pending_messages.update({key: event})
    runner._goal_max_turns_from_config = lambda: 50
    runner._goal_min_continuation_gap_from_config = lambda: 0
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    # The automatic turn did real (non-read-only) work, so it continues instead of
    # parking on the quiet no-progress ladder.
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "work remains", False, None, False)), \
            patch("hermes_cli.goals._evidence_only_read_only_status", return_value=False), \
            patch("hermes_cli.goals._goal_progress_fingerprint", side_effect=lambda _e: uuid.uuid4().hex):
        await runner._post_turn_goal_continuation(
            session_entry=runner.target, source=runner.target.origin, final_response="partial progress",
        )
    return runner.adapter._pending_messages[runner.target.session_key]


def _pause_before(action):
    """Resume lifts any pause; a replacement lifts only a judge pause over the contract it supersedes."""
    from hermes_cli.goals import GoalManager

    manager = GoalManager("target")
    if action != "replace":
        manager.pause()
        return
    manager.state.status, manager.state.last_verdict = "paused", "blocked"
    manager.state.paused_reason = "judge blocked: needs approval"
    manager._save()


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
    _pause_before(action)
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


@pytest.mark.asyncio
async def test_goal_continuation_rebinds_to_compression_child_after_rotation(state):
    from hermes_cli.goals import GoalManager, migrate_goal_to_session
    from hermes_cli.session_controls import refresh_goal_continuation

    runner = _runner(state)
    GoalManager("target").set("same objective")
    event = await _post_turn_event(runner)
    state.end_session("target", "compression")
    state.create_session(
        "compression-child", "telegram", parent_session_id="target", profile_name="default",
        chat_id="target", chat_type="private", session_key="agent:main:telegram:dm:target",
    )
    assert migrate_goal_to_session("target", "compression-child", reason="compression")

    assert refresh_goal_continuation(event, "compression-child") == event.text
    assert event.metadata["goal_continuation_session_id"] == "compression-child"
    child_goal = GoalManager("compression-child")
    dispatched_at = child_goal.state.last_automatic_continuation_at + 1.0
    assert child_goal.mark_automatic_continuation_dispatched(dispatched_at=dispatched_at)
    assert GoalManager("compression-child").state.last_automatic_continuation_at == dispatched_at


def test_dispatch_anchor_rejects_replaced_goal(state):
    from hermes_cli.goals import GoalManager

    manager = GoalManager("target")
    manager.set("old objective")
    old_instance = manager.state.created_at
    manager.clear()
    manager.set("replacement objective")

    live = GoalManager("target")
    assert not live.mark_automatic_continuation_dispatched(expected_instance=old_instance)
    assert GoalManager("target").state.last_automatic_continuation_at == 0.0


@pytest.mark.asyncio
async def test_loop_wake_rebinds_to_compression_child_after_rotation(state):
    from hermes_cli.loops import LoopManager, migrate_loop_to_session
    from hermes_cli.session_controls import loop_wake_metadata, loop_wake_is_current

    manager = LoopManager("target")
    manager.set("check the queue", interval_seconds=300)
    manager.state.next_due_at = 0
    manager._save()
    assert manager.fire_tick()
    event_metadata = loop_wake_metadata("target", manager.state)

    state.end_session("target", "compression")
    state.create_session(
        "compression-child", "telegram", parent_session_id="target", profile_name="default",
        chat_id="target", chat_type="private", session_key="agent:main:telegram:dm:target",
    )
    assert migrate_loop_to_session("target", "compression-child", reason="compression")

    assert loop_wake_is_current(event_metadata, "compression-child")
    assert event_metadata["loop_wakeup_session_id"] == "compression-child"


def _rotate_wake(state, kind, *, parent="target", child="compression-child",
                 reason="compression", marker=None):
    from hermes_cli.goals import migrate_goal_to_session
    from hermes_cli.loops import migrate_loop_to_session

    state.end_session(parent, reason)
    state.create_session(child, "telegram", parent_session_id=parent,
                         model_config={marker: parent} if marker else None,
                         profile_name="default", chat_id="target", chat_type="private",
                         session_key="agent:main:telegram:dm:target")
    migrate = migrate_goal_to_session if kind == "goal" else migrate_loop_to_session
    assert migrate(parent, child, reason="compression")
    return child


async def _queued_wake(runner, kind, strict):
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager
    from hermes_cli.session_controls import loop_wake_metadata

    if kind == "goal":
        GoalManager("target").set("watch the build")
        return await _post_turn_event(runner)
    manager = LoopManager("target")
    manager.set("check the queue", interval_seconds=300)
    manager.state.next_due_at = 0
    manager._save()
    prompt = manager.fire_tick()
    event = runner._synthetic_prompt_event(runner.target.origin, prompt, internal=True)
    event.metadata.update(loop_wake_metadata("target", manager.state))
    if strict:
        event.metadata.update(gateway_session_strict=True, gateway_session_id="target",
                              gateway_session_key=runner.target.session_key)
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("path", ["execution", "fifo"])
@pytest.mark.parametrize("strict", [False, True])
async def test_queued_wake_runs_and_settles_after_compression(state, kind, path, strict):
    from gateway.run_turn import GatewayTurnMixin
    from gateway.turn_context import TurnContext
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager

    runner = _runner(state)
    # Control continuations use the real strict producer and the fixture's typed authority.
    if kind == "goal" and strict:
        GoalManager("target").set("watch the build")
        GoalManager("target").pause()
        event = await _control_event(runner, _apply(state, "resume"))
    else:
        event = await _queued_wake(runner, kind, strict)
    identity = runner._post_turn_goal_identity(event)
    child = _rotate_wake(state, kind)
    runner.target.session_id = child
    runner.async_session_store = SimpleNamespace(
        lookup_by_session_key=AsyncMock(return_value=runner.target))
    if path == "execution":
        message = await GatewayTurnMixin._internal_wake_input_at_execution(
            runner, event, child, event.text)
        assert message == event.text
    else:
        runner._MAX_INTERRUPT_DEPTH = 5
        runner._session_key_for_source = lambda source: runner.target.session_key
        runner._prepare_profile_scoped_inbound_message_text = AsyncMock(
            side_effect=lambda **kw: kw["event"].text)
        runner._reply_anchor_for_event = lambda event: None
        runner._pinned_channel_inputs = lambda key, prompt, source, **kw: (prompt, source)
        runner._persist_prompt_pins = AsyncMock()
        runner._intake_adapter_for = lambda source: None
        runner._refresh_agent_cache_message_count = AsyncMock()
        runner._run_agent = AsyncMock(return_value={"final_response": "progress", "messages": []})
        ctx = TurnContext(source=runner.target.origin, session_key=runner.target.session_key,
                          session_id=child, run_generation=1, history=[])
        await GatewayTurnMixin._run_agent_queued_followup(
            runner, ctx, runner.adapter, event.text, event, "",
            {"interrupted": True, "messages": []}, None)
        runner._run_agent.assert_awaited_once()
        assert runner._run_agent.await_args.kwargs["session_id"] == child
    key = "goal_continuation_session_id" if kind == "goal" else "loop_wakeup_session_id"
    assert event.metadata[key] == child
    assert runner._post_turn_goal_identity(event) == identity
    if strict:
        assert event.metadata["gateway_session_id"] == child
        assert await GatewayTurnMixin._strict_session_pin_holds(runner, event, child)
    if kind == "loop":
        await runner._post_turn_loop_completion(session_entry=runner.target, source=event.source,
                                                final_response="progress", loop_tick=identity["loop_tick"])
        manager = LoopManager(child)
        assert manager.state.awaiting_response is False
        assert manager.state.ticks_fired == 1
        assert manager.fire_tick() is None
        manager.state.next_due_at = 0
        manager._save()
        assert manager.fire_tick()
        assert LoopManager(child).state.ticks_fired == 2
    else:
        assert GoalManager(child).state.created_at == identity["goal_instance"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
async def test_pre_turn_hygiene_compression_reaches_model_on_child(state, kind):
    from gateway.run_turn import GatewayTurnMixin
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    event = await _queued_wake(runner, kind, False)
    instance = runner._post_turn_goal_identity(event)

    async def hygiene(*args):
        # Compression owns the durable migration; hygiene publishes the new session id before
        # the model-boundary fence. Exercise the real handler across that exact await boundary.
        runner.target.session_id = _rotate_wake(state, kind)
        return []

    runner._hmwa_run_session_hygiene = AsyncMock(side_effect=hygiene)
    runner._PreparedTurn = GatewayTurnMixin._PreparedTurn

    async def prepare(*args):
        history = await runner._hmwa_run_session_hygiene(*args)
        return runner._PreparedTurn(history, "", event.text, None, None, None), None

    class ModelEntered(BaseException):
        pass

    runner._hmwa_resolve_session = AsyncMock(
        return_value=(event.source, runner.target, runner.target.session_key))
    runner._hmwa_prepare_turn = prepare
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner._is_user_turn_event = lambda event: False
    runner._revive_blocked_goal_for_user_turn = AsyncMock()
    runner._pinned_channel_inputs = lambda key, prompt, source, **kw: (prompt, source)
    runner._persist_prompt_pins = AsyncMock()
    runner._reply_anchor_for_event = lambda event: None
    runner._clear_session_env = lambda tokens: None
    runner._run_agent = AsyncMock(side_effect=ModelEntered)
    with pytest.raises(ModelEntered):
        await GatewayTurnMixin._handle_message_with_agent(runner, event, event.source, "k", 1)
    runner._hmwa_run_session_hygiene.assert_awaited_once()
    assert runner._run_agent.await_args.kwargs["session_id"] == "compression-child"
    metadata = runner._run_agent.await_args.kwargs["event_metadata"]
    assert metadata["goal_continuation_session_id" if kind == "goal" else "loop_wakeup_session_id"] == "compression-child"
    assert runner._post_turn_goal_identity(event) == instance
    if kind == "goal":
        assert GoalManager("compression-child").state.created_at == instance["goal_instance"]
    else:
        await runner._post_turn_loop_completion(session_entry=runner.target, source=event.source,
                                                final_response="progress", loop_tick=instance["loop_tick"])
        from hermes_cli.loops import LoopManager
        assert LoopManager("compression-child").state.awaiting_response is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("boundary", ["reset", "branch", "delegate", "marker_reset", "unrelated"])
async def test_compression_wake_rejects_noncontinuation_rebind(state, kind, boundary):
    from gateway.run_turn import GatewayTurnMixin

    runner = _runner(state)
    event = await _queued_wake(runner, kind, False)
    marker = {"branch": "_branched_from", "delegate": "_delegate_from", "marker_reset": "_reset_from"}.get(boundary)
    child = _rotate_wake(state, kind, reason="session_reset" if boundary == "reset" else "compression",
                         marker=marker)
    if boundary == "unrelated":
        state.create_session("resumed", "telegram")
        child = "resumed"
    event.metadata.update(gateway_session_strict=True, gateway_session_id="target",
                          gateway_session_key=runner.target.session_key)
    runner.target.session_id = child
    runner.async_session_store = SimpleNamespace(lookup_by_session_key=AsyncMock(return_value=runner.target))
    original = dict(event.metadata)
    assert not await GatewayTurnMixin._strict_session_pin_holds(runner, event, child)
    assert await GatewayTurnMixin._internal_wake_input_at_execution(runner, event, child, event.text) is None
    assert event.metadata == original


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "clear"])
async def test_compression_child_superseded_goal_still_drops_queued_wake(state, change):
    from gateway.run_turn import GatewayTurnMixin
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    event = await _queued_wake(runner, "goal", False)
    child = _rotate_wake(state, "goal")
    manager = GoalManager(child)
    manager.set("new objective") if change == "replace" else manager.clear()
    original = dict(event.metadata)
    assert await GatewayTurnMixin._internal_wake_input_at_execution(runner, event, child, event.text) is None
    assert event.metadata == original


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("depth,boundary,accepted", [
    (3, "inherited", True), (3, "reset", False), (3, "branch", False),
    (64, "compression", True), (65, "compression", False),
])
async def test_compression_wake_walks_only_bounded_continuation_edges(state, kind, depth, boundary, accepted):
    from hermes_cli.session_controls import internal_wake_is_current

    runner = _runner(state)
    event = await _queued_wake(runner, kind, False)
    parent = "target"
    for index in range(depth):
        parent = _rotate_wake(
            state, kind, parent=parent, child=f"compressed-{index}",
            reason="session_reset" if index == 1 and boundary == "reset" else "compression",
            marker="_branched_from" if index == 1 and boundary == "branch" else None)
        if boundary == "inherited":
            # Real compression copies model_config, so a marker for some older branch root
            # must not be confused with a fork from the immediate parent being walked.
            state._execute_write(lambda conn, sid=parent: conn.execute(
                "UPDATE sessions SET model_config = ? WHERE id = ?",
                ('{"_branched_from": "older-root"}', sid)))
    original = dict(event.metadata)
    assert internal_wake_is_current(event, parent) is accepted
    if accepted:
        key = "goal_continuation_session_id" if kind == "goal" else "loop_wakeup_session_id"
        assert event.metadata[key] == parent
    else:
        assert event.metadata == original


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "stop", "next_tick", "missing", "unreadable"])
async def test_compression_loop_requires_same_readable_live_tick(state, change):
    from hermes_cli.loops import LoopManager
    from hermes_cli.session_controls import loop_wake_is_current

    runner = _runner(state)
    event = await _queued_wake(runner, "loop", False)
    child = _rotate_wake(state, "loop")
    manager = LoopManager(child)
    if change == "replace":
        manager.set("different task", interval_seconds=300)
    elif change == "stop":
        manager.clear()
    elif change == "next_tick":
        manager.complete_tick("progress")
        manager.state.next_due_at = 0
        manager._save()
        manager.fire_tick()
    elif change == "missing":
        state._execute_write(lambda conn: conn.execute("DELETE FROM state_meta WHERE key = ?", (f"loop:{child}",)))
    else:
        state.set_meta(f"loop:{child}", "unreadable")
    original = dict(event.metadata)
    assert not loop_wake_is_current(event.metadata, child)
    assert event.metadata == original


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
    # Approved control continuations are strict: the drain re-checks the pin against the key's owner.
    if event.metadata.get("gateway_session_strict"):
        event.metadata["gateway_session_key"] = runner.target.session_key

    async def lookup_by_session_key(key):
        return runner.target if key == runner.target.session_key else None

    runner.async_session_store = SimpleNamespace(lookup_by_session_key=lookup_by_session_key)
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
    _pause_before(action)
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
    # The stale slot is removed; retained real work is promoted in FIFO order.
    slot = runner.adapter._pending_messages.get(runner.target.session_key)
    assert slot is not old
    assert ([slot] if slot is not None else []) + queue.conversation.queued_events == [fresh, human]


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
    assert after.turns_used == before.turns_used == (1 if change == "replace" else 0)
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
    manager = GoalManager("target")
    manager.state.last_automatic_continuation_at = 0.0
    manager._save()
    with patch("hermes_cli.goals.judge_goal",
               return_value=("continue", "work remains", False, None, False)) as judged, \
            patch("hermes_cli.goals._evidence_only_read_only_status", return_value=False), \
            patch("hermes_cli.goals._goal_progress_fingerprint", side_effect=lambda _e: uuid.uuid4().hex):
        await runner._run_post_turn_hooks(
            agent_result={"final_response": "progress on the new objective"},
            source=runner.target.origin, is_internal=True, event=head,
        )
    judged.assert_called_once()
    after = load_goal("target")
    assert after.goal == "new objective" and after.turns_used == before + 1
    assert runner.target.session_key in runner.adapter._pending_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("healed_to", ["other", "pinned"])
async def test_strict_event_is_dropped_when_topic_heal_moves_it_to_another_session(state, healed_to):
    """Topic-binding recovery must never carry a pinned continuation into a different session."""
    from types import MethodType
    from gateway.run_turn import GatewayTurnMixin
    from hermes_cli.goals import GoalManager

    from gateway.platforms.event import MessageEvent, MessageType

    runner = _runner(state)
    GoalManager("target").set("watch the build")
    goal_event = await _post_turn_event(runner)
    # A pinned plugin injection (e.g. a session-control requester notice) has no goal identity,
    # so only the strict session pin can keep it in its own conversation.
    event = MessageEvent(
        text="outcome notice", message_type=MessageType.TEXT, source=goal_event.source, internal=True,
        allow_gateway_control=False,
        metadata={"hermes_plugin_injection": True, "gateway_session_strict": True,
                  "gateway_session_key": runner.target.session_key, "gateway_session_id": "target"},
    )
    other = SimpleNamespace(session_id="other", session_key=runner.target.session_key)
    runner.async_session_store = SimpleNamespace(
        lookup_by_session_key=AsyncMock(return_value=runner.target))
    runner._recover_telegram_topic_thread_id = lambda source: None
    runner._cache_session_source = lambda key, source: None
    runner._is_telegram_topic_lane = lambda source: True
    runner._hmwa_heal_telegram_topic_binding = AsyncMock(
        return_value=other if healed_to == "other" else runner.target)
    runner._session_key_for_source = lambda source: runner.target.session_key
    resolved = await MethodType(GatewayTurnMixin._hmwa_resolve_session, runner)(event, event.source)
    if healed_to == "other":
        assert resolved is None
    else:
        assert resolved is not None and resolved[1].session_id == "target"


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["pause", "clear", "replace"])
async def test_continuation_superseded_during_preparation_never_reaches_the_model(state, control):
    """A control committed after admission and resolution, while the turn is being prepared."""
    from gateway.run_turn import GatewayTurnMixin
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    GoalManager("target").set("watch the build")
    event = await _post_turn_event(runner)
    message = await GatewayTurnMixin._internal_wake_input_at_execution(
        runner, event, "target", event.text)
    assert message == event.text  # still current: runs unchanged

    if control == "pause":
        GoalManager("target").pause()
    elif control == "clear":
        GoalManager("target").clear()
    else:
        _apply(state, "replace", goal="a different objective")
    assert await GatewayTurnMixin._internal_wake_input_at_execution(
        runner, event, "target", event.text) is None


@pytest.mark.asyncio
async def test_edited_goal_continuation_runs_its_refreshed_prompt_at_the_boundary(state):
    from gateway.run_turn import GatewayTurnMixin
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    GoalManager("target").set("watch the build")
    event = await _post_turn_event(runner)
    original = event.text
    GoalManager("target").add_subgoal("also check the deploy")
    message = await GatewayTurnMixin._internal_wake_input_at_execution(
        runner, event, "target", f"[context]\n{original}")
    assert message is not None and message != f"[context]\n{original}"
    assert "also check the deploy" in message and message.startswith("[context]\n")


@pytest.mark.asyncio
async def test_loop_wake_paused_during_preparation_never_reaches_the_model(state):
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run_turn import GatewayTurnMixin
    from hermes_cli.loops import LoopManager
    from hermes_cli.session_controls import loop_wake_metadata

    runner = _runner(state)
    mgr = LoopManager(session_id="target")
    mgr.set("poll CI", interval_seconds=300, route={"platform": "telegram", "chat_id": "1"})
    mgr.state.next_due_at = 0
    mgr._save()
    fired = LoopManager(session_id="target")
    assert fired.fire_tick() is not None
    event = MessageEvent(text="tick", message_type=MessageType.TEXT, source=runner.target.origin,
                         internal=True, metadata=loop_wake_metadata("target", fired.state))
    assert await GatewayTurnMixin._internal_wake_input_at_execution(runner, event, "target", "tick") == "tick"
    LoopManager(session_id="target").pause("approved in Telegram")
    assert await GatewayTurnMixin._internal_wake_input_at_execution(runner, event, "target", "tick") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["pause", "clear", "replace"])
async def test_fifo_continuation_superseded_inside_preparation_never_reaches_the_model(state, control):
    """Real producer (internal=False) and real FIFO path; the control lands during preparation."""
    from gateway.run_turn import GatewayTurnMixin
    from gateway.turn_context import TurnContext
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    GoalManager("target").set("watch the build")
    event = await _post_turn_event(runner)
    assert event.internal is False

    async def prepare(**kw):
        if control == "pause":
            GoalManager("target").pause()
        elif control == "clear":
            GoalManager("target").clear()
        else:
            _apply(state, "replace", goal="a different objective")
        return kw["event"].text

    runner._MAX_INTERRUPT_DEPTH = 5
    runner._session_key_for_source = lambda source: runner.target.session_key
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(side_effect=prepare)
    runner._reply_anchor_for_event = lambda event: None
    runner._pinned_channel_inputs = lambda key, prompt, source, **kw: (prompt, source)
    runner._persist_prompt_pins = AsyncMock()
    runner._intake_adapter_for = lambda source: None
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._run_agent = AsyncMock(return_value={"final_response": "ran", "messages": []})
    ctx = TurnContext(source=runner.target.origin, session_key=runner.target.session_key,
                      session_id="target", run_generation=1, history=[])
    await GatewayTurnMixin._run_agent_queued_followup(
        runner, ctx, runner.adapter, event.text, event, "", {"interrupted": True, "messages": []}, None,
    )
    runner._run_agent.assert_not_awaited()


# ── Compression between the scheduler's claim and adapter admission ─────────────────────────────
# fire_tick() / the post-turn judge stamp the wake with the parent session; compression can migrate
# the goal or loop to its continuation before admit_internal_event runs. Admission (no running
# session id yet) must follow the compression successor, and a refused admission must roll the
# claim back where it now lives, never leave the migrated loop awaiting a tick that never runs.

def _loop_fire_runner(runner, *, accept=True):
    from gateway.session import Platform, SessionSource

    async def handle(event):
        event._gateway_accepted = accept
        if event.text.lstrip().startswith("/"):
            # This fake handler represents the adapter's inline command dispatch receipt.
            event._gateway_inline_command_handled = True

    runner.adapter.handle_message = AsyncMock(side_effect=handle)
    runner._adapters_for_profile = lambda profile: {Platform.TELEGRAM: runner.adapter}
    runner._build_process_event_source = lambda evt: SessionSource(
        platform=Platform.TELEGRAM, chat_id=evt["chat_id"], chat_type=evt["chat_type"],
        user_id=evt["user_id"])
    runner._session_key_for_source = lambda source: runner.target.session_key
    runner._running_agents = {}
    return runner


async def _fire_then_compress(runner, state, prompt):
    import time
    from hermes_cli.loops import LoopManager

    manager = LoopManager("target")
    manager.set(prompt, interval_seconds=300, route={
        "platform": "telegram", "chat_id": "target", "chat_type": "dm", "user_id": "1"})
    manager.state.next_due_at = 0
    manager._save()
    real_fire = LoopManager.fire_tick

    def fire_then_compress(self, run_generation=0, *, can_claim=None):
        wakeup = real_fire(self, run_generation, can_claim=can_claim)
        _rotate_wake(state, "loop")  # compression lands between the claim and admission
        return wakeup

    with patch.object(LoopManager, "fire_tick", fire_then_compress):
        await runner._loop_wakeup_fire_one("target", LoopManager("target").state, time.time(), set())


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["check the queue", "/status"])
async def test_admission_follows_loop_migrated_after_fire_tick_and_settles_on_child(state, prompt):
    from hermes_cli.loops import LoopManager, load_loop

    runner = _loop_fire_runner(_runner(state))
    await _fire_then_compress(runner, state, prompt)
    runner.adapter.handle_message.assert_awaited_once()
    event = runner.adapter.handle_message.await_args.args[0]
    assert event.metadata["loop_wakeup_session_id"] == "compression-child"
    assert load_loop("target").status == "cleared"
    if prompt.startswith("/"):
        # A command tick is completed right after admission; that settlement must land on the child.
        child = load_loop("compression-child")
        assert child.awaiting_response is False and child.ticks_fired == 1
        return
    assert load_loop("compression-child").awaiting_response is True
    identity = runner._post_turn_goal_identity(event)
    await runner._post_turn_loop_completion(
        session_entry=SimpleNamespace(session_id="compression-child"), source=None,
        final_response="progress", loop_tick=identity["loop_tick"])
    child = load_loop("compression-child")
    assert child.awaiting_response is False and child.ticks_fired == 1
    assert LoopManager("compression-child").fire_tick() is None  # rescheduled, not wedged


@pytest.mark.asyncio
async def test_admission_refused_after_compression_skips_the_child_claim(state):
    from hermes_cli.loops import LoopManager, load_loop

    runner = _loop_fire_runner(_runner(state), accept=False)
    await _fire_then_compress(runner, state, "check the queue")
    runner.adapter.handle_message.assert_awaited_once()
    child = load_loop("compression-child")
    # fire_tick atomically starts the tick before admission. A later adapter refusal therefore
    # leaves the started tick for recovery, which skips it without replaying or refunding it.
    assert child.awaiting_response is True
    assert child.ticks_fired == 1
    manager = LoopManager("compression-child")
    assert manager.recover_tick()
    child = load_loop("compression-child")
    assert child.awaiting_response is False and child.ticks_fired == 1
    manager.state.next_due_at = 0
    manager._save()
    assert manager.fire_tick()  # the next cadence fires a fresh tick
    assert load_loop("compression-child").ticks_fired == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", ["post_turn", "control"])
async def test_admission_follows_goal_continuation_migrated_before_admit(state, producer):
    from gateway.wake import admit_internal_event
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    GoalManager("target").set("watch the build")
    if producer == "control":
        GoalManager("target").pause()
        event = await _control_event(runner, _apply(state, "resume"))
    else:
        event = await _post_turn_event(runner)
    child = _rotate_wake(state, "goal")

    async def accept(current):
        current._gateway_accepted = True

    runner.adapter.handle_message = AsyncMock(side_effect=accept)
    await admit_internal_event(runner.adapter, event)
    runner.adapter.handle_message.assert_awaited_once_with(event)
    assert event.metadata["goal_continuation_session_id"] == child
    if producer == "control":
        assert event.metadata["gateway_session_id"] == child


def test_lifted_wait_clear_follows_goal_migrated_before_admission(state):
    import time
    from hermes_cli.goals import GoalManager, load_goal

    manager = GoalManager("target")
    manager.set("watch the build")
    manager.state.waiting_until = time.time() - 1
    manager.state.waiting_since = 123.0
    manager._save()
    _rotate_wake(state, "goal")
    # The idle ticker loaded its manager on the parent; admission rebound the wake to the child.
    assert manager.clear_lifted_wait(123.0) is True
    assert not load_goal("compression-child").waiting_until


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
async def test_admission_inbound_preresolution_follows_migrated_wake(state, kind):
    from gateway.run_inbound import GatewayInboundMixin

    runner = _runner(state)
    event = await _queued_wake(runner, kind, False)
    child = _rotate_wake(state, kind)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda event, source: event)
    runner._is_user_authorized_for_source = lambda source: True
    runner._admit_bot_message_for_source = lambda source: True
    with patch("gateway.run_inbound._admit_outbox_event", new=AsyncMock()) as admit:
        admitted = await GatewayInboundMixin._hm_admit_event(runner, event)
    assert admitted is not None
    admit.assert_awaited_once()
    key = "goal_continuation_session_id" if kind == "goal" else "loop_wakeup_session_id"
    assert event.metadata[key] == child


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "pause_child", "stop_child", "stop_parent", "session_reset", "branch", "reset_marker", "ambiguous",
])
async def test_admission_still_drops_wake_superseded_by_user_or_non_compression(state, change):
    from gateway.wake import WakeSuperseded, admit_internal_event
    from hermes_cli.loops import LoopManager, load_loop

    runner = _runner(state)
    event = await _queued_wake(runner, "loop", False)
    if change == "stop_parent":
        LoopManager("target").clear()
    elif change in {"session_reset", "branch", "reset_marker"}:
        _rotate_wake(state, "loop", reason="session_reset" if change == "session_reset" else "compression",
                     marker={"branch": "_branched_from", "reset_marker": "_reset_from"}.get(change))
    else:
        child = _rotate_wake(state, "loop")
        if change == "pause_child":
            LoopManager(child).pause("user-paused")
        elif change == "stop_child":
            LoopManager(child).clear()
        else:
            state.create_session("stale-sibling", "telegram", parent_session_id="target",
                                 profile_name="default", chat_id="target", chat_type="private",
                                 session_key="agent:main:telegram:dm:target")
    runner.adapter.handle_message = AsyncMock()
    original = dict(event.metadata)
    with pytest.raises(WakeSuperseded):
        await admit_internal_event(runner.adapter, event)
    runner.adapter.handle_message.assert_not_awaited()
    assert event.metadata == original
    if change in {"pause_child", "stop_child"}:
        # A user stop/pause is not rolled back into a fresh claim either.
        LoopManager("target").abandon_tick()
        assert load_loop("compression-child").status == ("paused" if change == "pause_child" else "cleared")


def _topic_lookup(runner, monkeypatch, topics):
    from gateway import telegram_retire_work
    from hermes_platform import resolver

    lookup = AsyncMock(return_value=topics)
    runner.adapter._bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=456, username=None)),
        send_message=AsyncMock(),
    )
    monkeypatch.setattr(telegram_retire_work, "cli", lookup)
    monkeypatch.setattr(resolver, "locate_command", lambda _name: SimpleNamespace(command=("telegram",)))
    return lookup


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_state", ["absent", "paused", "superseded"])
async def test_ineligible_goal_does_not_probe_topic_after_turn(state, monkeypatch, goal_state):
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    manager = GoalManager("target")
    if goal_state != "absent":
        manager.set("finish the requested work")
        if goal_state == "paused":
            manager.pause()
    before = manager.state.to_json() if manager.state is not None else None
    runner._goal_max_turns_from_config = lambda: 50
    runner._goal_min_continuation_gap_from_config = lambda: 0
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    runner._session_key_for_source = lambda source: runner.target.session_key
    runner._enqueue_fifo = lambda key, event, adapter: adapter._pending_messages.update({key: event})
    lookup = _topic_lookup(runner, monkeypatch, [{"id": 42, "closed": False}])

    with patch("hermes_cli.goals.judge_goal") as judge:
        await runner._post_turn_goal_continuation(
            session_entry=runner.target, source=runner.target.origin, final_response="ordinary reply",
            goal_instance=-1 if goal_state == "superseded" else None,
        )

    lookup.assert_not_awaited()
    judge.assert_not_called()
    saved = GoalManager("target").state
    assert (saved.to_json() if saved is not None else None) == before
    assert runner.adapter._pending_messages == {} and runner.adapter.sends == []
    runner.adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", ["not_due", "busy", "in_flight"])
async def test_deferred_loop_does_not_probe_topic_on_scan(state, monkeypatch, deferred):
    from hermes_cli.loops import LoopManager

    runner = _loop_fire_runner(_runner(state))
    runner._build_process_event_source = lambda evt: runner.target.origin
    manager = LoopManager("target")
    manager.set("check the queue", interval_seconds=300, route={
        "platform": "telegram", "chat_id": "target", "chat_type": "dm",
        "thread_id": "42", "user_id": "1",
    })
    manager.state.next_due_at = 300 if deferred == "not_due" else 0
    manager._save()
    if deferred == "in_flight":
        manager.fire_tick()
    runner._is_session_running = lambda key: deferred != "not_due"
    before = manager.state.to_json()
    lookup = _topic_lookup(runner, monkeypatch, [{"id": 42, "closed": False}])

    await runner._loop_wakeup_fire_one("target", manager.state, 299, set())

    lookup.assert_not_awaited()
    runner.adapter.handle_message.assert_not_awaited()
    assert LoopManager("target").state.to_json() == before
    assert runner.adapter.sends == []
    runner.adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_retired_topic_post_turn_preserves_goal_without_judging(state, monkeypatch):
    from hermes_cli.goals import GoalManager

    runner = _runner(state)
    manager = GoalManager("target")
    manager.set("finish the requested work")
    before = manager.state.to_json()
    runner._goal_max_turns_from_config = lambda: 50
    runner._goal_min_continuation_gap_from_config = lambda: 0
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    queued = []
    runner._session_key_for_source = lambda source: runner.target.session_key
    runner._enqueue_fifo = lambda *args: queued.append(args)
    lookup = _topic_lookup(runner, monkeypatch, [])

    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "work remains", False, None, False)) as judge:
        await runner._post_turn_goal_continuation(
            session_entry=runner.target, source=runner.target.origin, final_response="partial progress",
        )

    judge.assert_not_called()
    lookup.assert_awaited_once_with(
        runner, runner.target.origin, "telegram", "--agent", "topic", "list", "456", "--id", "42",
    )
    assert GoalManager("target").state.to_json() == before
    assert queued == [] and runner.adapter.sends == []
    runner._defer_goal_status_notice_after_delivery.assert_not_awaited()
    runner.adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_retired_topic_loop_defers_before_claiming_tick(state, monkeypatch):
    from hermes_cli.loops import LoopManager

    runner = _loop_fire_runner(_runner(state))
    runner._build_process_event_source = lambda evt: runner.target.origin
    manager = LoopManager("target")
    manager.set("check the queue", interval_seconds=300, route={
        "platform": "telegram", "chat_id": "target", "chat_type": "dm",
        "thread_id": "42", "user_id": "1",
    })
    manager.state.next_due_at = 0
    manager._save()
    before = manager.state.to_json()
    lookup = _topic_lookup(runner, monkeypatch, [])

    await runner._loop_wakeup_fire_one("target", manager.state, 1e12, set())

    runner.adapter.handle_message.assert_not_awaited()
    lookup.assert_awaited_once_with(
        runner, runner.target.origin, "telegram", "--agent", "topic", "list", "456", "--id", "42",
    )
    assert LoopManager("target").state.to_json() == before
    assert runner.adapter.sends == []
    runner.adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("exists", [False, True], ids=["deleted", "live"])
async def test_topic_availability_rechecked_at_inbound_without_changing_state(state, monkeypatch, kind, exists):
    from gateway.run_inbound import GatewayInboundMixin
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager

    runner = _runner(state)
    event = await _queued_wake(runner, kind, False)
    manager_class = GoalManager if kind == "goal" else LoopManager
    before = manager_class("target").state.to_json()
    lookup = _topic_lookup(runner, monkeypatch, [{"id": 42, "closed": False}] if exists else [])
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda event, source: event)
    runner._is_user_authorized_for_source = lambda source: True
    runner._admit_bot_message_for_source = lambda source: True

    with patch("gateway.run_inbound._admit_outbox_event", new=AsyncMock()) as admit:
        admitted = await GatewayInboundMixin._hm_admit_event(runner, event)
    assert (admitted is not None) is exists
    if exists:
        admit.assert_awaited_once()
    else:
        admit.assert_not_awaited()
    lookup.assert_awaited_once_with(
        runner, runner.target.origin, "telegram", "--agent", "topic", "list", "456", "--id", "42",
    )
    assert manager_class("target").state.to_json() == before
    assert runner.adapter.sends == []
    runner.adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_topic_availability_does_not_gate_real_user_ingress(state, monkeypatch):
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run_inbound import GatewayInboundMixin

    runner = _runner(state)
    event = MessageEvent(text="continue the work", message_type=MessageType.TEXT, source=runner.target.origin)
    lookup = _topic_lookup(runner, monkeypatch, [])
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda event, source: event)
    runner._is_user_authorized_for_source = lambda source: True
    runner._admit_bot_message_for_source = lambda source: True

    with patch("gateway.run_inbound._admit_outbox_event", new=AsyncMock()) as admit:
        assert await GatewayInboundMixin._hm_admit_event(runner, event) is not None
    admit.assert_awaited_once()
    lookup.assert_not_awaited()
    runner.adapter._bot.send_message.assert_not_awaited()
