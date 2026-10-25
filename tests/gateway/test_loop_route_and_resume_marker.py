"""Loop wakeups across reversible route switches and stale restart markers.

Only an explicit /new retires a loop; /resume and topic rebinds point the route elsewhere
reversibly. A restart marker older than the auto-resume window must not wedge a loop.
"""
from datetime import datetime, timedelta

import pytest

from hermes_cli import loops
from tests.gateway.test_loop_interruption_recovery import create_loop, native_runner, scan
from tests.gateway.test_loop_interruption_recovery import control_home as loop_control_home  # noqa: F401 - registers the fixture

# The shared fixture points HERMES_HOME at tmp_path and owns the SessionDB cache lifecycle.
pytestmark = pytest.mark.usefixtures("loop_control_home")


@pytest.mark.asyncio
async def test_resume_elsewhere_defers_loop_until_route_returns(tmp_path):
    runner, _adapter, source, entry = native_runner(tmp_path)
    sid, key = entry.session_id, entry.session_key
    create_loop(entry, source)
    try:
        assert runner.session_store.switch_session(key, "20260101_000000_other") is not None
        await scan(runner, sid)
        saved = loops.load_loop(sid)
        assert saved.status == "active"
        assert saved.ticks_fired == 0 and not saved.awaiting_response

        assert runner.session_store.switch_session(key, sid) is not None
        await scan(runner, sid)
        saved = loops.load_loop(sid)
        assert saved.status == "active"
        assert saved.ticks_fired == 1 and saved.claim_phase == "started"
    finally:
        runner.session_store.close_all_db_handles()


def _age_marker(entry, age):
    entry.last_resume_marked_at = datetime.now() - age


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True])
async def test_stale_restart_marker_does_not_wedge_loop(tmp_path, started):
    runner, _adapter, source, entry = native_runner(tmp_path)
    sid, key = entry.session_id, entry.session_key
    mgr = create_loop(entry, source)
    if started:
        assert mgr.fire_tick()
    assert runner.session_store.mark_resume_pending(key, reason="restart_timeout")
    _age_marker(runner.session_store._entries[key], timedelta(days=3))
    try:
        await scan(runner, sid)
        saved = loops.load_loop(sid)
        assert not runner.session_store._entries[key].resume_pending
        assert saved.status == "active" and saved.ticks_fired == 1
        if started:
            assert not saved.awaiting_response  # skipped by recovery, never replayed
        else:
            assert saved.claim_phase == "started"
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_fresh_restart_marker_defers_due_loop(tmp_path):
    runner, _adapter, source, entry = native_runner(tmp_path)
    sid, key = entry.session_id, entry.session_key
    create_loop(entry, source)
    assert runner.session_store.mark_resume_pending(key, reason="restart_timeout")
    try:
        await scan(runner, sid)
        assert runner.session_store._entries[key].resume_pending
        assert loops.load_loop(sid).ticks_fired == 0
    finally:
        runner.session_store.close_all_db_handles()


def test_control_fence_is_per_home_and_retired_loops_fail_closed(tmp_path, monkeypatch):
    mgr = loops.LoopManager("shared-sid")
    mgr.set("watch", interval_seconds=60)
    home_fence = loops.loop_control_generation("shared-sid", default=None)
    assert home_fence is not None

    other = tmp_path / "other-profile"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert loops.loop_control_generation("shared-sid", default=None) is None
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert mgr.clear()
    # The synchronous execution fence must reject a retired loop's wake even at fence 0.
    assert loops.loop_control_generation("shared-sid", default=None) is None
