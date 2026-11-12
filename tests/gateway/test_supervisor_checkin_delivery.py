"""Execution attempts never masquerade as acknowledged supervisor output."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway import run_checkins
from gateway.checkin_delivery import delivery_metadata, fence_local_settlement, permits_metadata
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.session_results import execution_result
from hermes_cli.checkins import CheckinConfig, CheckinManager
from tests.gateway.test_supervisor_checkins import fixture as fixture  # noqa: PLC0414 -- fixture parameter intentionally shadows the imported pytest fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "held", "deferred", "interrupted"])
async def test_started_checkin_retains_visible_deadline_until_ack(fixture, outcome):
    f = fixture
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    before = f.manager.state
    f.clock.now = 1300
    await run_checkins.poll_checkins(f.runner)
    event = f.authority.admit_automation.await_args.args[1]
    assert await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)
    ack = {"failed": SendResult(success=False), "held": SendResult(success=True, held=True),
           "deferred": SendResult(success=True, deferred=True), "interrupted": None}[outcome]

    async def send(chat_id, content, reply_to=None, metadata=None):
        return ack

    f.adapter.send = send
    run_checkins.install_visible_observer(f.runner, f.adapter)
    if outcome == "interrupted":
        result = await run_checkins.silence_fallback(
            f.runner, event, f.entry, {"interrupted": True, "final_response": ""})
        assert result is not None
        assert result["final_response"] == ""
    else:
        await f.adapter.send("42", "not visible", metadata={"thread_id": "3"})
    assert f.manager.state.due_at == before.due_at
    assert f.manager.state.last_visible_at == before.last_visible_at
    # An ended or interrupted model attempt does not become one model call per heartbeat.
    # Recreating the manager and runner watch also retains this admission backoff.
    f.manager = CheckinManager(f.db, "owner", CheckinConfig())
    f.runner._checkin_seen, f.runner._checkin_watch = set(), {}
    f.authority.admit_automation.reset_mock()
    f.clock.now = 3099
    await run_checkins.poll_checkins(f.runner)
    assert not await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)
    f.authority.admit_automation.assert_not_awaited()
    assert f.manager.state.due_at == 1300
    # A native delivery retry may ACK the original result before another model attempt.
    ack = SendResult(success=True, message_id="visible")
    await f.adapter.send("42", "visible", metadata={"thread_id": "3"})
    assert f.manager.state.last_visible_at == 3099
    assert f.manager.state.due_at == 4899
    assert not await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)


@pytest.mark.asyncio
async def test_overdue_attempts_coalesce_at_recurring_admission_boundary(fixture):
    f = fixture
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    f.clock.now = 1300
    await run_checkins.poll_checkins(f.runner)
    event = f.authority.admit_automation.await_args.args[1]
    starts = await asyncio.gather(*(run_checkins.prepare_checkin(
        f.runner, event, f.entry, start=True) for _ in range(2)))
    assert starts.count(True) == 1
    assert f.manager.state.due_at == 1300
    f.authority.admit_automation.reset_mock()
    f.clock.now = 3100
    # Native running and pending admission guards still win after the backoff expires.
    f.runner._is_session_running = lambda _: True
    await run_checkins.poll_checkins(f.runner)
    f.runner._is_session_running = lambda _: False
    f.pending.append({"request_id": "first-attempt-still-pending"})
    await run_checkins.poll_checkins(f.runner)
    f.authority.admit_automation.assert_not_awaited()
    f.pending.clear()
    await run_checkins.poll_checkins(f.runner)
    f.authority.admit_automation.assert_awaited_once()
    retry = f.authority.admit_automation.await_args.args[1]
    assert retry.message_id != event.message_id
    assert retry.metadata["checkin"] == event.metadata["checkin"]
    assert await run_checkins.prepare_checkin(f.runner, retry, f.entry, start=True)
    assert not await run_checkins.prepare_checkin(f.runner, retry, f.entry, start=True)
    assert f.manager.state.due_at == 1300
    f.work.clear()
    await run_checkins.poll_checkins(f.runner)
    assert f.manager.state is None


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [
    {"completed": False, "partial": True, "failed": False, "failure_reason": "loop_error",
     "turn_exit_reason": "rebuilt_restart_limit_exceeded"},
    {"completed": False}, {"failed": True}, {"partial": True, "already_sent": True},
    {"failure_reason": "server_error"}, {"turn_exit_reason": "rebuilt_restart_limit_exceeded"},
])
@pytest.mark.parametrize("pending", [True, False])
async def test_failed_supervisor_refreshes_pending_status_and_durable_result(fixture, verdict, pending):
    f = fixture
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    event = MessageEvent(text="tick", source=f.source, internal=True,
                         metadata={"checkin": ["owner", 1300], "display_kind": "supervisor_checkin"})
    notice = "No reply: every provider in the fallback chain kept failing over."
    result = {"final_response": notice, "delivery_text": notice, "_model_final_response": notice,
              "api_calls": 3, **verdict}
    captured = {"result": dict(result)}
    token = execution_result.set(captured)
    try:
        # Human turns, queued human successors and real interruptions retain their own contract.
        for protected in ({**result, "interrupted": True},
                          {**result, "queued_terminal_display_kind": "human"}):
            assert await run_checkins.silence_fallback(f.runner, event, f.entry, protected) is protected
        human = MessageEvent(text="human", source=f.source)
        assert await run_checkins.silence_fallback(f.runner, human, f.entry, result) is result
        valid = {"completed": True, "final_response": "Tests are running. Next check in 30 minutes."}
        assert await run_checkins.silence_fallback(f.runner, event, f.entry, valid) is valid
        if not pending:
            f.work.clear()
            assert await run_checkins.silence_fallback(f.runner, event, f.entry, valid) is valid
        recovered = await run_checkins.silence_fallback(f.runner, event, f.entry, result)
        stored = captured["result"]
        for key, value in verdict.items():
            if key != "already_sent":
                assert stored[key] == value
        if pending:
            assert recovered is not None
            assert "Still outstanding: tests" in recovered["final_response"]
            assert "Next check in 30 minutes" in recovered["final_response"]
            assert not recovered["intentional_silence"]
            assert not recovered["already_sent"]
            assert {stored[k] for k in ("final_response", "delivery_text", "_model_final_response")} == {
                recovered["final_response"]}
        else:
            assert recovered["final_response"] == ""
            assert recovered["intentional_silence"] is True
            assert f.manager.state is None
            assert all(stored[k] == "" for k in ("final_response", "delivery_text", "_model_final_response"))
    finally:
        execution_result.reset(token)


@pytest.mark.asyncio
async def test_supervisor_delivers_final_after_verifying_last_dependency(fixture):
    f = fixture
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    f.clock.now = 1300
    await run_checkins.poll_checkins(f.runner)
    event = f.authority.admit_automation.await_args.args[1]
    assert await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)
    # The model verifies the last dependency during this admitted turn, then writes its result.
    assert f.work[0]["status"] == "running"
    f.work.clear()
    final = "Verified the last test result: all tests passed. The work is complete."
    result = {"completed": True, "failed": False, "final_response": final,
              "messages": [{"role": "assistant", "content": final}]}
    captured = {"result": dict(result)}
    token = execution_result.set(captured)
    try:
        delivered = await run_checkins.silence_fallback(f.runner, event, f.entry, result)
        assert delivered is result
        assert captured["result"]["final_response"] == final
        assert f.manager.state is None
    finally:
        execution_result.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [
    {"completed": True, "turn_exit_reason": "empty_response_exhausted", "failure_reason": "empty_response"},
    {"completed": True, "turn_exit_reason": "local_processing_error(details)", "failure_reason": "loop_error"},
    {"completed": True, "failure_reason": "session_persistence_failed:locked"},
    {"completed": False, "turn_exit_reason": "max_iterations_reached(3/3)"},
])
async def test_supervisor_preserves_written_advisory_and_handoff_status(fixture, verdict):
    f = fixture
    event = MessageEvent(text="tick", source=f.source, internal=True,
                         metadata={"checkin": ["owner", 1300], "display_kind": "supervisor_checkin"})
    text = "The test run is still running. Next check in 30 minutes."
    result = {"failed": False, "partial": False, "final_response": text,
              "_model_final_response": text, **verdict}
    for work in (list(f.work), []):
        f.work[:] = work
        assert await run_checkins.silence_fallback(f.runner, event, f.entry, result) is result


@pytest.mark.asyncio
async def test_storage_failure_uses_checkin_fallback_and_keeps_result_shape(request, monkeypatch):
    f = request.getfixturevalue("fixture")
    event = MessageEvent(text="tick", source=f.source, internal=True,
                         metadata={"checkin": ["owner", 1300], "display_kind": "supervisor_checkin"})
    event._checkin_fallback = "Roster storage is unavailable. Supervision will retry."
    result = {"completed": False, "final_response": "", "failed": True}
    monkeypatch.setattr("gateway.checkin_delivery.retain_policy",
                        lambda *args: (_ for _ in ()).throw(OSError("busy")))

    recovered = await run_checkins.silence_fallback(f.runner, event, f.entry, result)

    assert recovered["final_response"] == event._checkin_fallback
    assert recovered["delivery_text"] == event._checkin_fallback
    assert recovered["already_sent"] is False


@pytest.mark.asyncio
async def test_retention_failure_restores_scope_for_result_and_live_fence(fixture, monkeypatch):
    f = fixture
    f.work.clear()
    event = MessageEvent(text="tick", source=f.source, internal=True,
                         metadata={"checkin": ["owner", 1300], "display_kind": "supervisor_checkin"})
    event._gateway_active_turn_token = "turn-token"
    result = {"completed": True, "final_response": "NO_REPLY", "failed": False}
    captured = {"result": dict(result)}
    token = execution_result.set(captured)

    def fail_write(_write):
        raise OSError("busy")

    monkeypatch.setattr(f.db, "_execute_write", fail_write)
    try:
        recovered = await run_checkins.silence_fallback(f.runner, event, f.entry, result)
        assert event._checkin_pending_root == "owner"
        assert captured["result"]["checkin_pending_root"] == "owner"
        assert recovered["checkin_pending_root"] == "owner"

        monkeypatch.setattr(CheckinConfig, "load", classmethod(
            lambda cls: CheckinConfig(only_roots=("other",))))
        metadata = delivery_metadata(event, {})
        assert metadata["_checkin_scope"]["root"] == "owner"
        assert not await permits_metadata(f.runner, metadata)
        f.authority.runner = f.runner
        live = type("Live", (), {"source": f.source})()
        fenced = await fence_local_settlement(
            f.authority, live, {"payload": {"local_automation_v1": {"display_kind": "supervisor_checkin"}}},
            "pending", captured)
        assert fenced == ""
    finally:
        execution_result.reset(token)

@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raise", "none"])
async def test_unavailable_db_holds_known_supervisor_but_allows_ordinary(fixture, failure):
    f = fixture
    from gateway.delivery_recovery import _recover_obligation_policy

    def unavailable(_route):
        if failure != "none":
            raise OSError("transient open failure")

    f.runner.session_store._db_for_key = unavailable
    f.runner._release_runtime_claim_quiet = AsyncMock()
    blocked = {}
    supervisor = {"session_key": f.entry.session_key, "resume_turn_id": "token",
                  "obligation_id": "supervisor", "last_error": "send_path_degraded",
                  "supervisor_checkin": True}
    ordinary = {**supervisor, "obligation_id": "ordinary", "supervisor_checkin": False}

    assert await _recover_obligation_policy(f.runner, supervisor, "chat", blocked) is None
    assert "chat" in blocked
    assert await _recover_obligation_policy(f.runner, ordinary, "other-chat", {}) == {}
