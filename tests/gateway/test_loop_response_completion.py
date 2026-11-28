"""Final loop completion belongs to the loop definition seen by the answering turn."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.loop_completion import capture_loop_identity
from hermes_cli import loops
from tests.gateway.test_loop_command import _make_runner, _make_event

pytest_plugins = ["tests.gateway.test_loop_command"]


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["user", "goal", "relay"])
@pytest.mark.parametrize("phase", ["idle", "started", "abandoned"])
async def test_final_marker_stops_loop_from_any_owning_turn(loop_env, origin, phase):
    runner = _make_runner()
    event = _make_event("Check the task")
    event.internal = origin != "user"
    if origin == "goal":
        event.metadata = {"goal_continuation": True}
    elif origin == "relay":
        event.metadata = {"notification_origin": "relay_delivery"}
    manager = loops.LoopManager("sid-gateway-loop")
    manager.set("Check the deployment", interval_seconds=21600, times=12)
    if phase != "idle":
        assert manager.fire_tick()
        if phase == "abandoned":
            manager.abandon_tick()
    event._loop_completion_identity = capture_loop_identity(manager.session_id)
    await runner._run_post_turn_hooks(
        agent_result="The work is finished.\nLOOP_COMPLETE", source=event.source,
        is_internal=event.internal, event=event,
    )
    saved = loops.load_loop(manager.session_id)
    assert saved.status == "done"
    assert not saved.awaiting_response
    assert loops.list_active_loops() == []
    assert loops.LoopManager(manager.session_id).fire_tick() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revise", "replace", "pause", "resume", "clear", "other_session", "failed"])
async def test_final_marker_cannot_override_newer_controls(loop_env, change):
    runner = _make_runner()
    event = _make_event("Check the task")
    manager = loops.LoopManager("sid-gateway-loop")
    manager.set("Check the deployment", interval_seconds=21600, times=12)
    assert manager.fire_tick()
    event._loop_completion_identity = capture_loop_identity(manager.session_id)
    writer = loops.LoopManager(manager.session_id)

    def replace():
        prompt = "Watch the new deployment"
        quote = ("Please replace the loop prompt, set the interval to 10 minutes, run it 5 times, "
                 "and set the stop condition to ready")
        # Replace needs the user's quote in the current authenticated transcript.
        from hermes_cli import goals
        db = goals._get_session_db()
        db.create_session(session_id=manager.session_id, source="cli", model="test")
        db.append_message(manager.session_id, "user", quote)
        result = writer.replace(prompt=prompt, interval_seconds=600, reason="new task", user_quote=quote,
                                user_messages=[quote])
        assert result["ok"], result

    def resume():
        writer.pause()
        writer.resume()

    def other_session():
        other = loops.LoopManager("other")
        other.set("Other task", interval_seconds=300)
        event._loop_completion_identity = capture_loop_identity("other")

    {
        "revise": lambda: writer.revise(reason="Reduce polling", interval_seconds=43200)["ok"] or pytest.fail("revise"),
        "replace": replace,
        "pause": writer.pause,
        "resume": resume,
        "clear": writer.clear,
        "other_session": other_session,
        "failed": lambda: setattr(event, "_agent_turn_succeeded", False),
    }[change]()
    before = loops.load_loop(manager.session_id).to_json()
    runner._gateway_started_at = 0
    await runner._run_post_turn_hooks(
        agent_result="Task finished.\nLOOP_COMPLETE", source=event.source,
        is_internal=True, event=event,
    )
    assert loops.load_loop(manager.session_id).to_json() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["present", "absent", "nested_absent"])
async def test_queued_turn_carries_its_own_loop_definition(loop_env, terminal):
    from tests.gateway.test_goal_mutation_continuations import _runner

    runner, adapter, source, key, entry = _runner()
    manager = loops.LoopManager(entry.session_id)
    manager.set("Check the deployment", interval_seconds=300)
    expected = capture_loop_identity(entry.session_id)
    if terminal == "absent":
        manager.clear()
        expected = None
    reply = {"final_response": "Task complete.\nLOOP_COMPLETE", "messages": []}
    if terminal == "nested_absent":
        reply["queued_terminal_loop_completion_identity"] = None
        expected = None
    runner._run_agent = AsyncMock(return_value=reply)
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(
        side_effect=lambda **kwargs: kwargs["event"].text,
    )
    runner._pinned_channel_inputs = lambda key, prompt, source, **kwargs: (prompt, source)
    runner._persist_prompt_pins = AsyncMock()
    ctx = SimpleNamespace(
        source=source, session_id=entry.session_id, session_key=key, run_generation=1,
        _interrupt_depth=0, history=[], _status_thread_metadata={}, context_prompt=None,
        result_holder=[None], _post_delivery_adapter=adapter, channel_prompt=None,
    )
    event = _make_event("Check the deployment")
    event.source = source
    event.internal = True
    result = await runner._run_agent_queued_followup(
        ctx, adapter, event.text, event, "partial", {"interrupted": True, "messages": []}, None,
    )
    runner._run_agent.assert_awaited_once()
    assert result["queued_terminal_loop_completion_identity"] == expected


@pytest.mark.asyncio
async def test_silent_loop_complete_on_a_human_turn_sends_exactly_one_reply(loop_env, monkeypatch):
    """``[SILENT]`` + LOOP_COMPLETE from a person's turn finishes the loop. The finish notice is
    the turn's single reply: no "No reply was written" fallback, no second notice, no re-prompt."""
    from gateway.config import Platform
    from gateway.run_turn_silence_recovery import _completes_active_loop
    from gateway.session import SessionSource
    from gateway.platforms.event import MessageEvent

    runner = _make_runner()
    notices = AsyncMock()
    monkeypatch.setattr(runner, "_defer_goal_status_notice_after_delivery", notices, raising=False)
    monkeypatch.setattr(runner, "_room_allow_silence", lambda _source: None, raising=False)

    async def inline(func, *args):
        return func(*args)

    monkeypatch.setattr(runner, "_run_in_executor_with_context", inline, raising=False)
    source = SessionSource(platform=Platform.DISCORD, chat_id="dm-loop", chat_type="dm", user_id="user-loop")
    event = MessageEvent(text="Is it finished?", source=source, message_id="msg-dm")
    manager = loops.LoopManager("sid-gateway-loop")
    manager.set("Check the deployment", interval_seconds=21600, times=12)
    event._loop_completion_identity = capture_loop_identity(manager.session_id)
    raw = "[SILENT]\nLOOP_COMPLETE"
    assert _completes_active_loop(raw, manager.session_id)  # the one-shot silence re-prompt stands down

    session = SimpleNamespace(session_id=manager.session_id, incognito=False)
    response, silent, _ = await runner._hmwa_shape_agent_response(
        {"final_response": raw, "messages": [], "api_calls": 1}, source, [], session, None,
        None, 0, manager.session_id, "discord", 0.0,
        persist_user_display_kind=None, reply_expected=True,
        loop_identity=event._loop_completion_identity,
    )
    assert silent is False
    assert response == "✓ Loop finished — task complete."
    assert loops.load_loop(manager.session_id).status == "done"
    assert not _completes_active_loop(raw, manager.session_id)

    await runner._run_post_turn_hooks(agent_result=raw, source=source, is_internal=False, event=event)
    notices.assert_not_awaited()

    # Without an active loop the marker finishes nothing and the existing fallback remains.
    fallback, _, _ = await runner._hmwa_shape_agent_response(
        {"final_response": raw, "messages": [], "api_calls": 1}, source, [], session, None,
        None, 0, manager.session_id, "discord", 0.0,
        persist_user_display_kind=None, reply_expected=True,
        loop_identity=event._loop_completion_identity,
    )
    assert "No reply was written" in fallback
