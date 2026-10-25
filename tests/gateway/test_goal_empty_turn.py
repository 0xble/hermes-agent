"""Gateway goal admission follows the turn outcome, not visible reply length."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run_goals import GatewayGoalsMixin


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   ", "[SILENT]", "failure notice"])
@pytest.mark.parametrize("outcome", ["success", "failed", "interrupted", "incomplete", "legacy"])
async def test_post_turn_goal_admission_uses_outcome(text, outcome):
    runner = GatewayGoalsMixin()
    runner.async_session_store = SimpleNamespace(get_or_create_session=AsyncMock(
        return_value=SimpleNamespace(session_id="gateway-empty-turn")))
    runner._post_turn_goal_continuation = AsyncMock()
    runner._post_turn_loop_completion = AsyncMock()
    runner._is_user_turn_event = lambda event: True
    payload = {"final_response": text}
    event = SimpleNamespace(metadata={})
    fields, succeeded = {
        "success": ({"completed": True}, True),
        "failed": ({"failed": True}, False),
        "interrupted": ({"interrupted": True}, False),
        "incomplete": ({"completed": False}, None),
        "legacy": ({}, None),
    }[outcome]
    payload.update(fields)
    if succeeded is not None:
        event._agent_turn_succeeded = succeeded
    await runner._run_post_turn_hooks(agent_result=payload, source=None, is_internal=False, event=event)
    admitted = outcome == "success" or (outcome == "legacy" and bool(text.strip()))
    assert runner._post_turn_goal_continuation.await_count == int(admitted)
    runner._post_turn_loop_completion.assert_awaited_once()
    if admitted:
        assert runner._post_turn_goal_continuation.await_args.kwargs["final_response"] == text


pytest_plugins = ["tests.gateway.test_goal_verdict_send"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["empty", "whitespace", "tool_only", "failed", "interrupted", "incomplete"])
async def test_gateway_turn_evaluates_empty_success_and_releases_loop(hermes_home, monkeypatch, tmp_path, outcome):
    from hermes_cli import goals, loops
    from tests.gateway.test_duplicate_user_message import _bootstrap, _event
    from tests.hermes_cli.test_goal_judge_evidence import _record_tool_call
    from tests.gateway.test_goal_verdict_send import _RecordingAdapter

    runner = _bootstrap(monkeypatch, tmp_path)
    event = _event()
    entry = runner.session_store.get_or_create_session.return_value
    adapter = _RecordingAdapter()
    runner.adapters[event.source.platform] = adapter
    manager = goals.GoalManager(entry.session_id)
    manager.set("Verify the result")
    loop = loops.LoopManager(entry.session_id)
    loop.set("Check the deployment", interval_seconds=300, until="deployment healthy")
    assert loop.fire_tick()
    # Only the stamped wake settles a tick this process claimed; carry that identity.
    from hermes_cli.session_controls import loop_wake_metadata
    event.metadata = {**(event.metadata or {}), **loop_wake_metadata(entry.session_id, loop.state)}
    db = goals._get_session_db()
    db.ensure_session(entry.session_id, source="telegram")
    judged = []
    executed = []

    async def run_agent(**kwargs):
        executed.append(kwargs["inbound_message_id"])
        if outcome == "tool_only":
            _record_tool_call(db, entry.session_id, "terminal", {"command": "./bin/check"},
                              "Artifact verified", call_id="empty-tool")
        result = {"final_response": "   " if outcome == "whitespace" else "",
                  "completed": outcome in {"empty", "whitespace", "tool_only"},
                  "messages": [], "history_offset": 0}
        if outcome == "failed":
            result.update(failed=True, error="fixture provider failure")
        elif outcome == "interrupted":
            result.update(interrupted=True, final_response="Interrupted partial answer")
        elif outcome == "incomplete":
            result["final_response"] = "Incomplete response [LOOP_COMPLETE]"
        return result

    def judge(goal, response, **kwargs):
        judged.append((response, kwargs.get("evidence")))
        return "done", "runtime evidence verified", False, None, False

    runner._run_agent = run_agent
    monkeypatch.setattr(goals, "judge_goal", judge)
    await runner._handle_message(event)
    successful = outcome in {"empty", "whitespace", "tool_only"}
    after = goals.load_goal(entry.session_id)
    assert after.turns_used == int(successful)
    assert after.status == ("done" if successful else "active")
    assert len(judged) == int(successful)
    if successful:
        assert judged[0][0].strip() == ""
    if outcome == "tool_only":
        assert any("Artifact verified" in item["output"] for item in judged[0][1])
    loop_after = loops.load_loop(entry.session_id)
    assert loop_after.awaiting_response is False
    assert loop_after.status == "active"
    assert loop_after.ticks_fired == 1
    assert not adapter._pending_messages
    if outcome == "failed":
        assert adapter.sends and any(item["content"].strip() for item in adapter.sends)
    assert not runner._running_agents
    if successful:
        # The missing api_calls fixture field intentionally exercises display normalization's
        # cleanup notice. It is not evidence, nor a real blocked second turn.
        next_event = _event()
        next_event.message_id = "next-turn"
        await runner._handle_message(next_event)
        assert len(executed) == 2
        assert len(judged) == 1
        assert not runner._running_agents
        released = loops.load_loop(entry.session_id)
        assert released is not None and released.awaiting_response is False


@pytest.mark.parametrize("model_text", ["", "   ", "Verified.\nLOOP_COMPLETE"])
def test_post_turn_text_prefers_model_output_over_display_notice(model_text):
    event = SimpleNamespace(
        _agent_final_response=model_text,
        _raw_final_response="Display cleanup warning",
        _streamed_final_response="Display streamed warning",
    )
    assert GatewayGoalsMixin._final_text_for_post_turn_hooks("Display warning", event) == model_text


@pytest.mark.parametrize("model_text", ["", "   ", "[SILENT]", "Verified.\nLOOP_COMPLETE"])
def test_post_turn_dict_is_authoritative_over_legacy_display_stashes(model_text):
    event = SimpleNamespace(_raw_final_response="Display warning", _streamed_final_response="Stream warning")
    assert GatewayGoalsMixin._final_text_for_post_turn_hooks({"final_response": model_text}, event) == model_text


@pytest.mark.asyncio
@pytest.mark.parametrize("model_text", ["", "[SILENT]", "Verified.\nLOOP_COMPLETE"])
async def test_muted_diagnostic_turn_still_captures_model_text(monkeypatch, model_text):
    from gateway import warning_notifications
    from gateway.run_turn import GatewayTurnMixin

    # Delivery privacy is decided by cron_review.private_turn_response, which reads this helper.
    monkeypatch.setattr(warning_notifications, "diagnostic_wake_muted", lambda event: True)
    event = SimpleNamespace()
    result = {"final_response": model_text, "completed": True}
    delivered = await GatewayTurnMixin()._hmwa_deliver_turn_response(
        event, None, None, "sid", 1, result, [], "Display warning", "", False)
    assert delivered is None
    assert GatewayGoalsMixin._final_text_for_post_turn_hooks(delivered, event) == model_text


@pytest.mark.parametrize("model_text", ["", "   ", "[SILENT]"])
def test_run_sync_keeps_model_text_until_delivery(monkeypatch, model_text):
    from unittest.mock import MagicMock
    from gateway.run_turn_runner import TurnRunner
    from gateway.session import Platform, SessionSource
    from gateway.turn_context import TurnContext

    runner = MagicMock()
    runner._resolve_session_agent_runtime.return_value = ("fixture-model", {})
    ctx = TurnContext(source=SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u"),
                      message="verify", history=[], session_id="sid", session_key="sk", user_config={})
    turn = TurnRunner(runner, ctx)
    agent = SimpleNamespace(model="fixture-model")
    ctx.agent_holder[0] = agent
    monkeypatch.setattr(turn, "_combined_ephemeral_prompt", lambda: "")
    monkeypatch.setattr(turn, "_setup_stream_consumer", lambda *a: (None, None, None, False))
    monkeypatch.setattr(turn, "_resolve_turn_agent", lambda *a: (agent, False))
    monkeypatch.setattr(turn, "_wire_turn_agent_callbacks", lambda *a: None)
    monkeypatch.setattr(turn, "_load_turn_history", lambda *a: ([], None, []))
    monkeypatch.setattr(turn, "_prepare_turn_message", lambda *a: ("verify", None))
    monkeypatch.setattr(turn, "_run_conversation_with_approval", lambda *a: {
        "final_response": model_text, "completed": True, "messages": []})
    monkeypatch.setattr(turn, "_finish_stream_consumer", lambda *a: None)
    monkeypatch.setattr(turn, "_sync_session_after_run", lambda *a: (False, "sid", 0))
    result = turn.run_sync()
    assert result["completed"] is True
    assert result["final_response"] == model_text
    assert result["_model_final_response"] == model_text

    if model_text:
        # Gateway media decoration remains a delivery feature, not judge evidence.
        monkeypatch.setattr(turn, "_finish_stream_consumer", lambda result, *a:
                            result.update(final_response="Repaired media copy"))
        monkeypatch.setattr(turn, "_append_auto_media_tags", lambda *a: "Display MEDIA:/fixture.png")
        decorated = turn.run_sync()
        assert decorated["final_response"] == "Display MEDIA:/fixture.png"
        assert GatewayGoalsMixin._final_text_for_post_turn_hooks(decorated) == model_text
