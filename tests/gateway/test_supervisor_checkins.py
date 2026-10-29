"""Idle scheduling, FIFO priority, visibility and generated mandatory-reply contracts."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.response_filters import display_kind_for_event
from gateway.session import SessionSource
from gateway import run_checkins
from hermes_cli.checkins import CheckinConfig, CheckinManager
from hermes_state import SessionDB


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("owner", source="telegram")
    clock = SimpleNamespace(now=100.0)
    time = SimpleNamespace(time=lambda: clock.now)
    monkeypatch.setattr(run_checkins, "time", time)
    monkeypatch.setattr("hermes_cli.checkins.time", time)
    monkeypatch.setattr(CheckinConfig, "load", classmethod(lambda cls: CheckinConfig()))
    monkeypatch.setattr("agent.estop.paused_reply", lambda: None)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="7", thread_id="3", message_id="human")
    entry = SimpleNamespace(session_key="topic-route", session_id="owner", origin=source, suspended=False)
    store = SimpleNamespace(list_sessions=lambda: [entry], lookup_by_session_key=lambda _: entry,
                            _db_for_key=lambda _: db)
    adapter = SimpleNamespace(_pending_messages={}, _text_batches={}, _active_sessions={})
    runner = SimpleNamespace(session_store=store, _draining=False, _is_session_running=lambda _: False,
                             _restored_source=lambda _: source, _async_profile_scope_for_source=lambda _: nullcontext(),
                             _run_in_executor_with_context=asyncio.to_thread, _delivery_adapter_for=lambda _: adapter,
                             _is_intentional_silence=lambda result, text: text.strip() in {"NO_REPLY", "[SILENT]"})
    pending = []
    authority = SimpleNamespace(db=db, logical_owner=lambda _: "owner", admit_automation=AsyncMock())
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda _: authority)
    monkeypatch.setattr("hermes_state_runtime.list_session_admissions", lambda *a, **k: list(pending))
    work = [{"kind": "delegation", "id": "child", "name": "tests", "status": "running", "started_at": 100}]
    monkeypatch.setattr(run_checkins, "outstanding_work", lambda *args: list(work))
    yield SimpleNamespace(db=db, clock=clock, runner=runner, source=source, entry=entry, adapter=adapter,
                          authority=authority, pending=pending, work=work,
                          manager=CheckinManager(db, "owner", CheckinConfig()))
    db.close()


@pytest.mark.asyncio
async def test_end_turn_arms_then_idle_tick_preserves_topic_and_clears_stale_anchor(fixture):
    f = fixture
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    assert f.manager.state.due_at == 1300
    f.clock.now = 1299
    await run_checkins.poll_checkins(f.runner)
    f.authority.admit_automation.assert_not_awaited()
    f.clock.now = 1300
    await run_checkins.poll_checkins(f.runner)
    adapter, event, identity = f.authority.admit_automation.await_args.args
    assert adapter is f.adapter
    assert event.source.thread_id == "3" and event.source.chat_id == "42"
    assert event.source.message_id is None and f.source.message_id == "human"
    assert event.internal and display_kind_for_event(event) == "supervisor_checkin"
    assert event.metadata["checkin"] == ["owner", 1300]
    assert identity.startswith('["supervisor-checkin",')


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["runner", "adapter", "pending-message", "text-batch", "human-fifo", "heartbeat-fifo"])
async def test_busy_or_queued_input_defers_tick(fixture, busy):
    f = fixture
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    f.clock.now = 1400
    if busy == "runner":
        f.runner._is_session_running = lambda _: True
    elif busy == "adapter":
        f.adapter._active_sessions[f.entry.session_key] = True
    elif busy == "pending-message":
        f.adapter._pending_messages[f.entry.session_key] = "human"
    elif busy == "text-batch":
        f.adapter._text_batches[f.entry.session_key] = "human"
    else:
        f.pending.append({"request_id": busy})
    await run_checkins.poll_checkins(f.runner)
    f.authority.admit_automation.assert_not_awaited()
    assert f.manager.state.due_at == 1300


@pytest.mark.asyncio
async def test_no_work_never_arms_and_completion_disarms_without_final_tick(fixture):
    f = fixture
    f.work.clear()
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    assert f.manager.state is None
    f.work.append({"id": "child"})
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    f.work.clear()
    f.clock.now = 1400
    await run_checkins.poll_checkins(f.runner)
    assert f.manager.state is None
    f.authority.admit_automation.assert_not_awaited()


@pytest.mark.asyncio
async def test_execution_time_fence_drops_completion_or_visible_reset_after_admission(fixture):
    f = fixture
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    f.clock.now = 1300
    await run_checkins.poll_checkins(f.runner)
    event = f.authority.admit_automation.await_args.args[1]
    f.manager.visible(1300)
    assert not await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)
    # A completion arriving after the poll is also checked at consumption, not only admission.
    f.work.clear()
    assert not await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)
    assert f.manager.state is None


@pytest.mark.asyncio
async def test_queued_user_wins_at_execution_time_and_active_goal_does_not_defer(fixture):
    f = fixture
    f.db.set_meta("goal:owner", '{"goal":"ship", "status":"active"}')
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    f.clock.now = 1300
    await run_checkins.poll_checkins(f.runner)
    event = f.authority.admit_automation.await_args.args[1]
    f.pending.append({"request_id": "new-human"})
    assert not await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)
    f.pending.clear()
    assert await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)
    assert "Inspect its live logs" in event.text
    assert f.manager.state.fired
    assert not await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)


@pytest.mark.asyncio
async def test_watch_restores_after_restart_without_using_heartbeat_or_loop_slot(fixture):
    f = fixture
    f.manager.reconcile(f.work, now=100)
    # The runner has neither a watch nor a seen list, as after process restart.
    f.clock.now = 1500
    await run_checkins.poll_checkins(f.runner)
    f.authority.admit_automation.assert_awaited_once()
    assert not f.db.get_meta("heartbeat:owner") and not f.db.get_meta("loop:owner")


@pytest.mark.asyncio
async def test_registry_error_retains_supervision_for_next_scan(fixture, monkeypatch):
    f = fixture
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    def unavailable(*args):
        raise OSError("registry locked")
    monkeypatch.setattr(run_checkins, "outstanding_work", unavailable)
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    assert f.entry.session_key in f.runner._checkin_watch
    assert f.manager.state.due_at == 1300


@pytest.mark.asyncio
async def test_send_and_streamed_edit_ack_reset_correct_topic_only(fixture):
    f = fixture
    sibling = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="7", thread_id="4")
    f.db.create_session("sibling", source="telegram")
    other = SimpleNamespace(session_key="sibling-route", session_id="sibling", origin=sibling, suspended=False)
    f.runner.session_store.list_sessions = lambda: [f.entry, other]
    f.runner._restored_source = lambda entry: entry.origin
    other_manager = CheckinManager(f.db, "sibling", CheckinConfig())
    f.manager.reconcile([1], now=100)
    other_manager.reconcile([1], now=100)
    ack = SimpleNamespace(result=SendResult(success=True, message_id="sent"))
    async def send(chat_id, content, reply_to=None, metadata=None):
        return ack.result
    async def edit_message(chat_id, message_id, content, *, metadata=None, finalize=False):
        return ack.result
    f.adapter.send, f.adapter.edit_message = send, edit_message
    run_checkins.install_visible_observer(f.runner, f.adapter)
    f.clock.now = 200
    await f.adapter.send("42", "visible", metadata={"thread_id": "3"})
    assert f.manager.state.due_at == 1400
    assert other_manager.state.due_at == 1300
    f.clock.now = 220
    await f.adapter.edit_message("42", "sent", "streamed")
    assert f.manager.state.due_at == 1420
    f.clock.now = 230
    await f.adapter.edit_message("42", "sent", "streamed")
    assert f.manager.state.due_at == 1420
    for failed in (SendResult(success=False), SendResult(success=True, deferred=True), SendResult(success=True, held=True)):
        ack.result = failed
        f.clock.now = 400
        assert await f.adapter.send("42", "not visible", metadata={"thread_id": "3"}) is failed
        assert f.manager.state.due_at == 1420


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["stop", "startup"])
async def test_supervisor_respects_user_stop_and_startup_recovery(fixture, boundary):
    f = fixture
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    f.clock.now = 1400
    if boundary == "stop":
        f.runner._user_stop_latched = lambda *_: True
    else:
        f.runner._startup_restore_in_progress = True
    await run_checkins.poll_checkins(f.runner)
    f.authority.admit_automation.assert_not_awaited()
    assert f.manager.state.due_at == 1300


@pytest.mark.asyncio
async def test_visible_output_before_turn_ends_is_the_first_cadence_anchor(fixture):
    f = fixture
    await run_checkins.record_visible(f.runner, f.adapter, "42", "3")
    f.clock.now = 500
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    assert f.manager.state.due_at == 1300


@pytest.mark.asyncio
async def test_unexecuted_admission_does_not_tombstone_the_due_check(fixture):
    f = fixture
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    f.clock.now = 1400
    await run_checkins.poll_checkins(f.runner)
    await run_checkins.poll_checkins(f.runner)
    first, second = f.authority.admit_automation.await_args_list
    assert first.args[2] != second.args[2]
    assert first.args[1].metadata["checkin"] == second.args[1].metadata["checkin"]
    assert f.manager.state.due_at == 1300


def test_checkin_descriptor_is_not_accepted_from_human_metadata(fixture):
    event = MessageEvent(text="human", source=fixture.source,
                         metadata={"checkin": ["owner", 1300], "display_kind": "supervisor_checkin"})
    assert run_checkins.descriptor(event) is None


@pytest.mark.asyncio
async def test_silence_is_reprompted_once_and_second_refusal_gets_normal_delivery_fallback(fixture):
    from gateway.run_turn_silence_recovery import silence_recovery_due
    f = fixture
    ctx = SimpleNamespace(persist_user_display_kind="supervisor_checkin", reply_expected=False,
                          _run_still_current=lambda: True)
    response = {"final_response": "NO_REPLY", "completed": True, "acknowledged_by_reaction": True}
    assert silence_recovery_due(f.runner, response, ctx, silence_retry=False, scheduled_heartbeat=False)
    assert not silence_recovery_due(f.runner, response, ctx, silence_retry=True, scheduled_heartbeat=False)
    ctx.persist_user_display_kind = "internal_notification"
    assert not silence_recovery_due(f.runner, response, ctx, silence_retry=False, scheduled_heartbeat=False)
    event = MessageEvent(text="tick", source=f.source, internal=True,
                         metadata={"checkin": ["owner", 1300], "display_kind": "supervisor_checkin"})
    result = await run_checkins.silence_fallback(f.runner, event, f.entry, response)
    assert "Still outstanding: tests" in result["final_response"]
    assert "Next check in 30 minutes" in result["final_response"]
    assert "NO_REPLY" not in result["final_response"]


def test_native_and_local_restore_preserve_generated_descriptor(fixture):
    from gateway.session_automation import automation_checkin_metadata
    from gateway.session_envelope import restore_native
    f = fixture
    payload = {"text": "[Supervisor check-in]", "native_text_v1": {
        "source": {**f.source.to_dict(), "is_bot": False}, "timestamp": "1970-01-01T00:00:00+00:00",
        "event": {"message_id": "tick"}, "route": f.entry.session_key,
        "automation": {"owner": "owner", "checkin": ["owner", 1300],
                       "display_kind": "supervisor_checkin", "display_text": "Supervisor check-in"}}}
    event = restore_native(payload)
    assert event.internal and event.metadata["checkin"] == ["owner", 1300]
    assert display_kind_for_event(event) == "supervisor_checkin"
    assert event.source.thread_id == "3"
    assert automation_checkin_metadata(payload["native_text_v1"]["automation"]) == {"checkin": ["owner", 1300]}
