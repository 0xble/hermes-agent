"""Supervisor normalization must not bypass the gateway's durable turn lifecycle."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import run_checkins
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from gateway.session_results import execution_result, prepare_result
from hermes_cli.checkins import CheckinConfig, CheckinManager, outstanding_work


@pytest.fixture
def supervisor_turn(tmp_path, monkeypatch, request):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=getattr(request, "param", Platform.TELEGRAM), chat_id="42", user_id="7", thread_id="3")
    entry = store.get_or_create_session(source)
    db = store._db_for_session_id(entry.session_id)
    work = [{"kind": "delegation", "id": "last-child", "name": "tests", "status": "running"}]
    monkeypatch.setattr(run_checkins, "outstanding_work", lambda *_: list(work))
    monkeypatch.setattr(CheckinConfig, "load", classmethod(lambda cls: CheckinConfig()))
    clock = SimpleNamespace(now=1300)
    monkeypatch.setattr(run_checkins, "time", SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr("hermes_cli.checkins.time", SimpleNamespace(time=lambda: clock.now))
    manager = CheckinManager(db, entry.session_id, CheckinConfig())
    manager.reconcile(work, now=100)
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda _: SimpleNamespace(
        db=db, logical_owner=lambda sid: sid))
    monkeypatch.setattr("hermes_state_runtime.list_session_admissions", lambda *a, **k: [])
    monkeypatch.setattr("gateway.run_heartbeat_acceptance.heartbeat_owner_is_current", lambda *_: True)
    monkeypatch.setattr("gateway.run._load_gateway_config", dict)
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "test-model")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = store
    runner._session_db = None
    adapter = SimpleNamespace(_pending_messages={}, _text_batches={}, _active_sessions={})
    runner._delivery_adapter_for = lambda _: adapter
    runner._run_in_executor_with_context = asyncio.to_thread
    runner._hmwa_resolve_session = AsyncMock(return_value=(source, entry, entry.session_key))
    runner._prepare_loop_wake_execution = AsyncMock(return_value=True)
    runner._hmwa_prepare_turn = AsyncMock(return_value=(runner._PreparedTurn(
        [], "", "supervisor tick", None, None, "supervisor_checkin", entry.session_id, "input-owner"), []))
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner._revive_blocked_goal_for_user_turn = AsyncMock()
    runner._pinned_channel_inputs = lambda *a, **k: (None, source)
    runner._reply_anchor_for_event = lambda _: None
    runner._hmwa_stop_typing_for_turn = AsyncMock()
    runner._is_user_turn_event = lambda _: False
    runner._is_session_run_current = lambda *a: True
    runner._clear_restart_failure_count = AsyncMock()
    runner._hmwa_prepend_reasoning = lambda _, response, *a: response
    runner._hmwa_runtime_footer_line = lambda *a: None
    async def no_compression(result, response, session_entry, *a):
        return response, session_entry
    runner._hmwa_compression_exhaustion_reset = no_compression
    runner._drain_watch_notifications = AsyncMock()
    runner._hmwa_store_turn_prompt_tokens = AsyncMock()
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._should_send_voice_reply = lambda *a, **k: False
    runner._clear_session_env = lambda _: None
    f = SimpleNamespace(store=store, source=source, entry=entry, db=db, work=work,
                        manager=manager, runner=runner, clock=clock)
    yield f
    f.db.close()


def tick(f, index=1):
    f.store.mark_resume_pending(f.entry.session_key, turn_id=f"turn-{index}", human=False)
    return MessageEvent(text="tick", source=f.source, internal=True, message_id=f"supervisor-{index}",
                        metadata={"checkin": [f.manager.root, f.manager.state.due_at],
                                  "display_kind": "supervisor_checkin"})


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["final", "failed-empty", "silent-empty", "failed-pending", "written-pending"])
async def test_supervisor_normalized_outcome_closes_and_persists_turn(supervisor_turn, outcome):
    f = supervisor_turn
    store, source, entry, db, work, manager, runner = (
        f.store, f.source, f.entry, f.db, f.work, f.manager, f.runner)
    event = tick(f)
    final = "Verified the last test result: all tests passed. The work is complete."
    captured = {}
    async def model(**kwargs):
        assert work and manager.state.fired
        if not outcome.endswith("pending"):
            # The model observes/verifies the last dependency before composing its final.
            work.clear()
        result = {"agent_persisted": False, "completed": outcome in {"final", "silent-empty", "written-pending"},
                  "failed": outcome.startswith("failed"), "messages": [], "api_calls": 1,
                  "final_response": final if outcome == "final" else (
                      "NO_REPLY" if outcome == "silent-empty" else (
                          "The build finished. Artifact verification is running." if outcome == "written-pending"
                          else "Every provider failed."))}
        if not result["failed"]:
            result["messages"] = [{"role": "user", "content": kwargs["message"]},
                                  {"role": "assistant", "content": result["final_response"]}]
        if result["failed"]:
            result["error"] = "provider unavailable"
        captured["result"] = dict(result)
        return result
    runner._run_agent = model
    token = execution_result.set(captured)
    try:
        response = await runner._handle_message_with_agent(event, source, entry.session_key, 1)
        saved = captured["result"]
        hooks = [args.args[1]["response"] for args in runner.hooks.emit.await_args_list
                 if args.args[0] == "agent:end"]
        rows = [row for row in db.get_messages(entry.session_id) if row["role"] != "session_meta"]
        assert [row["role"] for row in rows] == ["user", "assistant"]
        assert event._agent_turn_succeeded is (not outcome.startswith("failed"))
        if event._agent_turn_succeeded:
            assert store.get_resume_pending_marker(entry.session_key) is None
        else:
            assert store.get_resume_pending_marker(entry.session_key) is not None
        if outcome == "final":
            assert response == final
            assert saved["final_response"] == final
            assert rows[-1]["content"] == final
            assert hooks == [final]
        elif outcome == "written-pending":
            assert response == "The build finished. Artifact verification is running."
            assert saved["final_response"] == response and rows[-1]["content"] == response
            assert hooks == [response]
        elif outcome == "failed-pending":
            assert "Still outstanding: tests" in response
            assert hooks == [response]
            assert saved["final_response"] == response
            assert saved["failed"] is True
            assert rows[-1]["content"] == response
        else:
            assert not response
            assert saved["final_response"] == ""
            assert saved["intentional_silence"] is True
            assert hooks == [""]
        if outcome.startswith("failed"):
            assert rows[-1]["display_kind"] == "failed_turn"
        assert (manager.state is not None) is outcome.endswith("pending")
        # The same normalized capture is what native durable completion/replay compacts.
        _, durable = prepare_result({"payload": {"text": "tick"}}, response, "completed", captured)
        assert durable["result"]["final_response"] == saved["final_response"]
    finally:
        execution_result.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("supervisor_turn", [Platform.TELEGRAM, Platform.LOCAL], indirect=True)
@pytest.mark.parametrize("marker", ["NO_REPLY", "[SILENT]"])
async def test_quiet_peer_inspections_retain_work_and_bound_attempts_after_reload(
        supervisor_turn, monkeypatch, marker):
    """Drive the real gateway handler, peer registry, cadence and durable result boundary."""
    from gateway.session_context import scoped_current_session_id
    from hermes_cli import goals
    from hermes_state import SessionDB

    f = supervisor_turn
    monkeypatch.setattr(goals, "_get_session_db", lambda: f.db)
    monkeypatch.setattr("tools.async_delegation.supervisor_inventory", lambda _: [])
    monkeypatch.setattr("tools.process_registry.process_registry._running", {})
    monkeypatch.setattr(run_checkins, "outstanding_work", outstanding_work)
    goal = goals.GoalManager(f.entry.session_id)
    goal.set("Verify the Maintenance cutover")
    with scoped_current_session_id(f.entry.session_id):
        assert goal.wait_on_peer("hermes:default/maintenance", "cutover", "cutover at a known future window",
                                 expected_instance=goal.state.created_at)["ok"]
    original_goal = f.db.get_meta("goal:" + f.entry.session_id)
    f.manager.reconcile([], now=100)
    before = f.manager.reconcile(outstanding_work(f.db, f.entry.session_id), now=100)
    captured, calls = {}, []

    async def model(**kwargs):
        calls.append(kwargs)
        result = {"completed": True, "failed": False, "agent_persisted": False, "api_calls": 1,
                  "final_response": marker, "messages": [
                      {"role": "user", "content": kwargs["message"]},
                      {"role": "assistant", "content": marker}]}
        captured["result"] = dict(result)
        return result

    f.runner._run_agent = model
    f.runner._restored_source = lambda entry: entry.origin
    f.runner._async_profile_scope_for_source = lambda _: nullcontext()
    f.runner._is_session_running = lambda _: False
    authority = SimpleNamespace(db=f.db, logical_owner=lambda sid: sid, admit_automation=AsyncMock())
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda _: authority)
    token = execution_result.set(captured)
    try:
        for index, now in enumerate((1300, 3100), 1):
            f.clock.now = now
            event = tick(f, index)
            response = await f.runner._handle_message_with_agent(event, f.source, f.entry.session_key, 1)
            assert not response
            assert len(calls) == index  # No continuation or model replay for deliberate quiet.
            assert event._agent_turn_succeeded
            assert f.store.get_resume_pending_marker(f.entry.session_key) is None
            saved = captured["result"]
            assert saved["intentional_silence"] is True and saved["final_response"] == ""
            outcome, durable = prepare_result({"payload": {"text": "tick"}}, response, "completed", captured)
            assert outcome == "completed" and durable["result"]["final_response"] == ""
            assert durable["result"]["intentional_silence"] is True
            await run_checkins.record_local_settlement(f.runner, f.source, {"outcome": outcome}, response, captured)
            assert f.db.get_meta("goal:" + f.entry.session_id) == original_goal
            state = f.manager.state
            assert state.last_visible_at == before.last_visible_at and state.due_at == before.due_at
            assert state.next_attempt_at == now + 1800 and state.fired
            assert not await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)
            # Reopen persistence and discard runner watches, as after a restart.
            path = f.db.db_path
            f.db.close()
            f.db = SessionDB(db_path=path)
            authority.db = f.db
            f.store._db = f.db
            f.manager = CheckinManager(f.db, f.entry.session_id, CheckinConfig())
            f.runner._checkin_watch, f.runner._checkin_seen = {}, set()
            f.clock.now = now + 1799
            authority.admit_automation.reset_mock()
            await run_checkins.poll_checkins(f.runner)
            authority.admit_automation.assert_not_awaited()
            assert not await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)
            assert f.manager.state == state
        hooks = [call.args[1]["response"] for call in f.runner.hooks.emit.await_args_list
                 if call.args[0] == "agent:end"]
        assert hooks == ["", ""]
        rows = [row for row in f.db.get_messages(f.entry.session_id) if row["role"] != "session_meta"]
        assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant"]
        assert [row["content"] for row in rows if row["role"] == "assistant"] == [marker, marker]
    finally:
        execution_result.reset(token)


@pytest.mark.asyncio
async def test_supervisor_status_replaces_the_agent_persisted_failed_boundary(tmp_path, monkeypatch):
    """The normal agent path: the real AIAgent flushes the turn and the real core closer
    (``_close_durable_failed_turn``) writes "Send it again" before the gateway runs. The gateway
    must turn THIS boundary into the normalized supervisor status in place, not keep the generic
    copy and not stack a second assistant row."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent.conversation_loop import _close_durable_failed_turn
    from agent.turn_failure_copy import FAILED_TURN_NOTICE
    from run_agent import AIAgent
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="7", thread_id="3")
    entry = store.get_or_create_session(source)
    db = store._db_for_session_id(entry.session_id)
    db.append_message(entry.session_id, "user", "earlier request")
    db.append_message(entry.session_id, "assistant", FAILED_TURN_NOTICE, display_kind="failed_turn")
    work = [{"kind": "delegation", "id": "last-child", "name": "tests", "status": "running"}]
    monkeypatch.setattr(run_checkins, "outstanding_work", lambda *_: list(work))
    monkeypatch.setattr(CheckinConfig, "load", classmethod(lambda cls: CheckinConfig()))
    monkeypatch.setattr(run_checkins, "time", SimpleNamespace(time=lambda: 1300))
    manager = CheckinManager(db, entry.session_id, CheckinConfig())
    manager.reconcile(work, now=100)
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda _: SimpleNamespace(
        db=db, logical_owner=lambda sid: sid))
    monkeypatch.setattr("hermes_state_runtime.list_session_admissions", lambda *a, **k: [])
    monkeypatch.setattr("gateway.run_heartbeat_acceptance.heartbeat_owner_is_current", lambda *_: True)
    monkeypatch.setattr("gateway.run._load_gateway_config", dict)
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "test-model")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = store
    runner._session_db = db
    adapter = SimpleNamespace(_pending_messages={}, _text_batches={}, _active_sessions={})
    runner._delivery_adapter_for = lambda _: adapter
    runner._run_in_executor_with_context = asyncio.to_thread
    runner._hmwa_resolve_session = AsyncMock(return_value=(source, entry, entry.session_key))
    runner._prepare_loop_wake_execution = AsyncMock(return_value=True)
    history = [{"role": "user", "content": "earlier request"},
               {"role": "assistant", "content": FAILED_TURN_NOTICE}]
    runner._hmwa_prepare_turn = AsyncMock(return_value=(runner._PreparedTurn(
        history, "", "supervisor tick", None, None, "supervisor_checkin", entry.session_id, "input-owner"), []))
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner._revive_blocked_goal_for_user_turn = AsyncMock()
    runner._pinned_channel_inputs = lambda *a, **k: (None, source)
    runner._reply_anchor_for_event = lambda _: None
    runner._hmwa_stop_typing_for_turn = AsyncMock()
    runner._is_user_turn_event = lambda _: False
    runner._is_session_run_current = lambda *a: True
    runner._clear_restart_failure_count = AsyncMock()
    runner._hmwa_prepend_reasoning = lambda _, response, *a: response
    runner._hmwa_runtime_footer_line = lambda *a: None
    async def no_compression(result, response, session_entry, *a):
        return response, session_entry
    runner._hmwa_compression_exhaustion_reset = no_compression
    runner._drain_watch_notifications = AsyncMock()
    runner._hmwa_store_turn_prompt_tokens = AsyncMock()
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._should_send_voice_reply = lambda *a, **k: False
    runner._clear_session_env = lambda _: None
    store.mark_resume_pending(entry.session_key, turn_id="this-turn", human=False)
    event = MessageEvent(text="tick", source=source, internal=True, message_id="supervisor-1",
                         metadata={"checkin": [manager.root, 1300], "display_kind": "supervisor_checkin"})
    captured = {}

    def agent_turn(message):
        agent = AIAgent(session_db=db, session_id=entry.session_id, model="test-model", provider="openai-compat",
                        api_key="test", base_url="http://127.0.0.1:1/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=True)
        # The agent starts from the durable history it was handed (already persisted, so the flush
        # only writes this turn's accepted user row), exactly as the gateway's run passes it.
        conversation_history = [dict(row) for row in history]
        messages = conversation_history + [{"role": "user", "content": message}]
        agent._flush_messages_to_session_db(messages, conversation_history)  # turn-start flush
        result = {"completed": False, "failed": True, "error": "provider unavailable", "api_calls": 1,
                  "final_response": "Every provider failed.", "messages": messages, "history_offset": len(history),
                  "current_turn_user_idx": len(history)}
        _close_durable_failed_turn(agent, result)  # real core closer: "Send it again" boundary
        assert messages[-1]["content"] == FAILED_TURN_NOTICE
        return result

    async def model(**kwargs):
        result = await asyncio.to_thread(agent_turn, kwargs["message"])
        captured["result"] = dict(result)
        return result
    runner._run_agent = model
    token = execution_result.set(captured)
    try:
        response = await runner._handle_message_with_agent(event, source, entry.session_key, 1)
        saved = captured["result"]
        hooks = [args.args[1]["response"] for args in runner.hooks.emit.await_args_list
                 if args.args[0] == "agent:end"]
        rows = [row for row in db.get_messages(entry.session_id) if row["role"] != "session_meta"]
        assert "Still outstanding: tests" in response
        assert hooks == [response]
        assert saved["final_response"] == response
        assert saved["failed"] is True
        assert event._agent_turn_succeeded is False
        # One boundary for this turn, now carrying the delivered status; the earlier turn's
        # boundary is untouched.
        assert [(row["role"], row.get("display_kind")) for row in rows] == [
            ("user", None), ("assistant", "failed_turn"), ("user", None), ("assistant", "failed_turn")]
        assert rows[1]["content"] == FAILED_TURN_NOTICE
        assert rows[-1]["content"] == response
    finally:
        execution_result.reset(token)
        db.close()
