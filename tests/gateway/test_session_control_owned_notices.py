"""Agent-owned controls settle cosmetic receipts without starting a redundant self-turn."""
import pytest

from tests.gateway.test_session_control_watcher import _runner, state as state


@pytest.mark.asyncio
@pytest.mark.parametrize("autonomy", [False, True])
@pytest.mark.parametrize("notices", ["hidden", False, True])
@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
async def test_owned_loop_controls_never_inject_their_own_outcome(state, autonomy, notices, action):
    from hermes_constants import get_hermes_home
    from hermes_cli import session_controls as controls
    from hermes_cli.loops import LoopManager

    get_hermes_home().joinpath("config.yaml").write_text(
        f"loops:\n  autonomy: {str(autonomy).lower()}\n  notices: {str(notices).lower()}\n"
    )
    manager = LoopManager("target")
    manager.set("Watch the build", actor="agent")
    if action == "resume":
        manager.pause("Wait for build", by="agent")
    result = controls.apply_control("loop", action, "target", requester_sid="target",
                                    reason="Build status changed")
    assert result["ok"], result
    assert result["record"]["authority"]["via"] == "agent"
    runner = _runner(state)
    await runner._drain_session_controls()
    await runner._drain_session_controls()

    assert runner.injections == []
    assert runner.adapter.requests == []
    assert len(runner.adapter.sends) == int(notices is True)
    if notices is True:
        assert "(applied by the owning agent without approval)" in runner.adapter.sends[0][1]
        assert "authority source not recorded" not in runner.adapter.sends[0][1]
    persisted = controls._load_record(result["record"]["id"])
    assert persisted["requester_notification_skipped"]
    assert persisted["target_notice_skipped"] is (notices is not True)
    assert persisted["outbox_done"]
    assert controls.pending_outbox() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("notices", ["hidden", False, True])
@pytest.mark.parametrize("self_target", [False, True])
async def test_legacy_autonomy_receipts_keep_cross_session_notices_only(state, notices, self_target):
    from hermes_constants import get_hermes_home
    from hermes_cli import session_controls as controls
    from hermes_cli.loops import LoopManager

    get_hermes_home().joinpath("config.yaml").write_text(
        f"loops:\n  autonomy: true\n  notices: {str(notices).lower()}\n"
    )
    # User-created state exercises the autonomy path, not the owned fast path.
    LoopManager("target").set("Watch the build")
    result = controls.apply_control("loop", "pause", "target",
                                    requester_sid="target" if self_target else "requester",
                                    reason="Wait for build")
    assert result["ok"], result
    assert result["record"]["authority"]["via"] == "autonomy"
    runner = _runner(state)
    await runner._drain_session_controls()
    await runner._drain_session_controls()
    assert len(runner.injections) == int(notices is True and not self_target)
    assert len(runner.adapter.sends) == int(notices is True)
    if notices is True:
        assert "applied autonomously without approval" in runner.adapter.sends[0][1]
        if not self_target:
            assert "was applied autonomously without approval" in runner.injections[0]["content"]
    assert controls._load_record(result["record"]["id"])["outbox_done"]
    assert controls.pending_outbox() == []
