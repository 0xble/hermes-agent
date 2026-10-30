"""An unavailable local roster cannot silence an already-armed supervisor."""
import pytest

from gateway import run_checkins
from tests.gateway.test_supervisor_checkins import fixture  # noqa: F401 -- native scheduler fixture


def unavailable(*args):
    raise OSError("local roster unavailable")


@pytest.mark.asyncio
async def test_armed_roster_error_keeps_due_clock_and_delivers_unknown_status(fixture, monkeypatch):
    f = fixture
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    before = f.manager.state
    monkeypatch.setattr(run_checkins, "outstanding_work", unavailable)
    f.clock.now = 1299
    await run_checkins.poll_checkins(f.runner)
    assert f.manager.state == before
    f.authority.admit_automation.assert_not_awaited()
    # Simulate restart, keeping only the persisted CheckinState.
    f.runner._checkin_seen = set()
    f.runner._checkin_watch = {}
    await run_checkins.poll_checkins(f.runner)
    assert f.manager.state == before
    f.clock.now = 1300
    await run_checkins.poll_checkins(f.runner)
    f.authority.admit_automation.assert_awaited_once()
    event = f.authority.admit_automation.await_args.args[1]
    assert await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)
    assert "unknown" in event.text.lower() and "roster" in event.text.lower()
    result = await run_checkins.silence_fallback(f.runner, event, f.entry, {"final_response": "NO_REPLY"})
    assert "unknown" in result["final_response"].lower()


@pytest.mark.asyncio
async def test_unarmed_roster_error_stays_silent(fixture, monkeypatch):
    f = fixture
    monkeypatch.setattr(run_checkins, "outstanding_work", unavailable)
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    f.clock.now = 5000
    await run_checkins.poll_checkins(f.runner)
    assert f.manager.state is None
    assert not getattr(f.runner, "_checkin_watch", {})
    f.authority.admit_automation.assert_not_awaited()


@pytest.mark.asyncio
async def test_armed_observation_error_only_confirmed_delivery_resets(fixture, monkeypatch):
    f = fixture
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    before = f.manager.state
    monkeypatch.setattr(run_checkins, "outstanding_work", unavailable)
    from gateway.platforms.base import SendResult
    ack = SendResult(success=False)

    async def send(chat_id, content, reply_to=None, metadata=None):
        return ack

    f.adapter.send = send
    run_checkins.install_visible_observer(f.runner, f.adapter)
    f.clock.now = 1300
    await run_checkins.poll_checkins(f.runner)
    event = f.authority.admit_automation.await_args.args[1]
    assert await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)
    f.authority.admit_automation.reset_mock()
    f.clock.now = 1400
    await f.adapter.send("42", "not visible", metadata={"thread_id": "3"})
    await run_checkins.poll_checkins(f.runner)
    assert f.manager.state.due_at == before.due_at
    assert f.manager.state.last_visible_at == before.last_visible_at
    ack = SendResult(success=True, message_id="visible")
    await f.adapter.send("42", "visible", metadata={"thread_id": "3"})
    assert f.manager.state.due_at == 3200
    f.clock.now = 3199
    await run_checkins.poll_checkins(f.runner)
    f.authority.admit_automation.assert_not_awaited()
    # A successful empty roster disarms, rather than generating a no-work update.
    monkeypatch.setattr(run_checkins, "outstanding_work", lambda *a: [])
    await run_checkins.poll_checkins(f.runner)
    assert f.manager.state is None
