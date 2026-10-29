"""Execution attempts never masquerade as acknowledged supervisor output."""
import asyncio

import pytest

from gateway import run_checkins
from gateway.platforms.base import SendResult
from hermes_cli.checkins import CheckinConfig, CheckinManager
from tests.gateway.test_supervisor_checkins import fixture


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
