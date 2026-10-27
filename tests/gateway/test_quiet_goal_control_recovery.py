"""Quiet cosmetic outcomes must not change durable control admission or recovery."""
import time
from unittest.mock import AsyncMock, patch

import pytest

from tests.gateway.test_session_control_watcher import _runner, state as state


async def _accept(_adapter, event):
    event._gateway_accepted = True


def _policy(autonomy=True, notices=False):
    from hermes_constants import get_hermes_home
    get_hermes_home().joinpath("config.yaml").write_text(
        f"goals:\n  autonomy: {str(autonomy).lower()}\n  notices: {str(notices).lower()}\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("notices", [False, True])
async def test_failed_autonomy_outcome_uses_visibility_policy(state, notices):
    from hermes_cli import session_controls as controls
    from hermes_cli.goals import GoalManager
    _policy(notices=notices)
    GoalManager("target").set("already active")
    result = controls.apply_control("goal", "resume", "target", requester_sid="requester")
    assert result["status"] == "failed"
    runner = _runner(state)
    await runner._drain_session_controls()
    await runner._drain_session_controls()
    assert len(runner.adapter.sends) == len(runner.injections) == int(notices)
    persisted = controls._load_record(result["record"]["id"])
    assert persisted["outbox_done"]
    assert persisted["target_notice_skipped"] is not notices
    assert persisted["requester_notification_skipped"] is not notices
    assert controls.pending_outbox() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "resume", "replace"])
@pytest.mark.parametrize("user_quote", ["", "check progress"])
async def test_quiet_online_autonomy_settles_notices_and_keeps_progress(state, action, user_quote):
    from hermes_cli import session_controls as controls
    from hermes_cli.goals import GoalManager
    _policy()
    manager = GoalManager("target")
    manager.set("old objective")
    if action == "resume":
        manager.pause()
    result = controls.apply_control("goal", action, "target", requester_sid="requester",
                                    payload={"goal": "new objective"} if action == "replace" else None,
                                    user_quote=user_quote)
    assert result["status"] == "applied"
    record = controls._load_record(result["record"]["id"])
    assert record["authority"] == {"via": "autonomy", "session_id": "requester", "quote": user_quote}
    runner = _runner(state)
    with patch("gateway.wake.admit_internal_event", new=AsyncMock(side_effect=_accept)):
        await runner._drain_session_controls()
        await runner._drain_session_controls()
    persisted = controls._load_record(record["id"])
    assert persisted["authority"] == record["authority"]
    assert persisted["outbox_done"]
    assert persisted["continuations_cleared"] and persisted["continuation_enqueued"]
    assert persisted["target_notice_skipped"] and persisted["requester_notification_skipped"]
    assert runner.adapter.sends == runner.injections == []
    assert controls.pending_outbox() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "resume", "replace"])
async def test_quiet_offline_control_recovers_progress_without_cosmetic_retries(state, action):
    from hermes_cli import session_controls as controls
    from hermes_cli.goals import GoalManager, load_goal
    _policy()
    manager = GoalManager("target")
    manager.set("old objective")
    if action == "resume":
        manager.pause()
    result = controls.apply_control("goal", action, "target", requester_sid="requester",
                                    payload={"goal": "new objective"} if action == "replace" else None)
    assert result["status"] == "applied"
    offline = _runner(state, target_route=False, send_success=False, injection_result=False)
    for _ in range(2):
        await offline._drain_session_controls()
    persisted = controls._load_record(result["record"]["id"])
    assert persisted["target_notice_skipped"] and persisted["requester_notification_skipped"]
    assert offline.adapter.sends == offline.injections == []
    if action != "pause":
        assert persisted["continuation_enqueued"] is False
        assert controls.pending_outbox()
    # A later policy change must not replay already-settled cosmetic outcomes.
    _policy(notices=True)
    online = _runner(state)
    with patch("gateway.wake.admit_internal_event", new=AsyncMock(side_effect=_accept)) as admit:
        await online._drain_session_controls()
        await online._drain_session_controls()
    assert admit.await_count == int(action != "pause")
    assert online.adapter.sends == online.injections == []
    assert controls.pending_outbox() == []
    # An autonomous pause parks the goal on a timed wake instead of stopping it.
    final = load_goal("target")
    assert final.status == "active"
    assert (final.waiting_until > time.time()) is (action == "pause")
    assert controls._load_record(result["record"]["id"])["continuations_cleared"]


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["quote", "button", "session"])
async def test_quiet_profile_keeps_explicit_authority_replies(state, authority):
    from hermes_cli import session_controls as controls
    from hermes_cli.goals import GoalManager
    _policy(autonomy=False)
    GoalManager("target").set("old objective")
    if authority == "button":
        request = controls.request_control("goal", "pause", "target", requester_sid="requester")
        pending = _runner(state)
        await pending._drain_session_controls()
        assert len(pending.adapter.requests) == 1
        result = controls.resolve_request(request["id"], "approve", "admin")
    elif authority == "quote":
        quote = "Please pause the target goal now"
        state.append_message("requester", "user", quote)
        result = controls.apply_control("goal", "pause", "target", requester_sid="requester",
                                        user_quote=quote)["record"]
    else:
        result = controls.apply_control("goal", "replace", "target", requester_sid="requester",
                                        payload={"goal": "new objective"})["record"]
    assert result["authority"]["via"] == authority
    runner = _runner(state)
    # A replace queues a continuation; notices follow only its accepted admission.
    with patch("gateway.wake.admit_internal_event", new=AsyncMock(side_effect=_accept)):
        await runner._drain_session_controls()
    assert len(runner.adapter.sends) == len(runner.injections) == 1
    assert controls.pending_outbox() == []


@pytest.mark.asyncio
async def test_quiet_goal_policy_keeps_loop_control_outcome(state):
    from hermes_cli import session_controls as controls
    from hermes_cli.loops import LoopManager
    _policy()
    LoopManager("target").set("check status")
    quote = "Please pause the target loop right now"
    state.append_message("requester", "user", quote)
    result = controls.apply_control("loop", "pause", "target", requester_sid="requester", user_quote=quote)
    assert result["status"] == "applied"
    runner = _runner(state)
    await runner._drain_session_controls()
    assert len(runner.adapter.sends) == len(runner.injections) == 1
    assert controls.pending_outbox() == []
