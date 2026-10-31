"""notices: hidden keeps every goal and loop card out of chat, and wakes the agent on a stall."""
import json
from types import SimpleNamespace

import pytest

import hermes_bootstrap

from agent.inline_tool_executors import _goal_receipt_text
from tests.gateway.test_goal_notices_config import runner_and_source


@pytest.fixture
def hidden_home(tmp_path, monkeypatch):
    """Set both levels through the real ``hermes config set`` path, then open its SessionDB."""
    from pathlib import Path
    from hermes_cli import config, goals
    from hermes_cli.goal_notices import notice_level
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config.set_config_value("goals.notices", "hidden")
    config.set_config_value("loops.notices", "hidden")
    assert notice_level("goals") == notice_level("loops") == "hidden"
    goals._DB_CACHE.clear()
    db = goals._get_session_db()
    db.create_session("target", "telegram", profile_name="default", chat_id="target",
                      chat_type="private", session_key="agent:main:telegram:dm:target")
    yield tmp_path
    goals._DB_CACHE.clear()


def test_config_set_stores_the_hidden_level(hidden_home):
    import hermes_yaml as yaml
    data = yaml.safe_load((hidden_home / "config.yaml").read_text())
    assert data["goals"]["notices"] == "hidden" and data["loops"]["notices"] == "hidden"


@pytest.mark.asyncio
async def test_hidden_stall_cards_wake_the_session_durably(hidden_home):
    from hermes_cli import goals, lifecycle_wakes
    runner, adapter, source = runner_and_source()
    key = runner._session_key_for_source(source)
    runner.session_store = SimpleNamespace(
        lookup_by_session_key=lambda k: SimpleNamespace(session_id="target") if k == key else None)

    async def run_in_executor(fn, *args):
        return fn(*args)

    runner._run_in_executor_with_context = run_in_executor
    stalls = ["⏸ Goal blocked — paused: no route", "✗ Goal failed: judge unavailable"]
    for message in stalls:
        await runner._defer_goal_status_notice_after_delivery(source, message)
    await runner._send_goal_status_notice(source, "⏸ Goal paused: ship it", notice_kind="paused", stall=False)
    await runner._send_goal_status_notice(source, "✓ Goal achieved: shipped")
    await runner._defer_goal_status_notice_after_delivery(
        source, "⏳ Loop still active — stop condition judged unachievable: no canary", section="loops", stall=True)
    await runner._defer_goal_status_notice_after_delivery(
        source, "✓ Loop finished after 3 ticks — task complete.", section="loops", stall=False)
    await runner._defer_goal_status_notice_after_delivery(source, stalls[0])  # repeat: no second wake
    receipt = json.dumps({"success": True, "persisted": True, "notice": "Loop set"})
    assert _goal_receipt_text("loop_set", receipt) == "" and _goal_receipt_text("goal_set", receipt) == ""
    assert adapter.calls == [] and adapter.callbacks == {}

    goals._DB_CACHE.clear()  # a restart keeps only what SessionDB holds
    queued = lifecycle_wakes.pending()
    assert sorted(r["message"] for r in queued) == sorted(
        [*stalls, "⏳ Loop still active — stop condition judged unachievable: no canary"])

    injected = []

    async def inject(**kwargs):
        injected.append(kwargs)
        return True

    runner._dispatch_plugin_message_injection = inject
    await runner._drain_lifecycle_wakes()
    assert {(i["session_key"], i["expected_session_id"]) for i in injected} == {(key, "target")}
    assert all("has stalled" in i["content"] and "hidden from the user" in i["content"] for i in injected)
    assert lifecycle_wakes.pending() == []
    await runner._defer_goal_status_notice_after_delivery(source, stalls[0])
    assert lifecycle_wakes.pending() == []  # the delivered wake blocks a rewake for the window


@pytest.mark.parametrize("kind,action", [("goal", "pause"), ("loop", "stop")])
def test_hidden_approval_cards_refuse_instead_of_waiting(hidden_home, kind, action):
    from hermes_cli import goals, loops, session_controls
    if kind == "goal":
        goals.GoalManager("target").set("Ship the repair")
    else:
        loops.LoopManager("target").set("watch", interval_seconds=300)
    result = session_controls.request_control(kind, action, "target", requester_sid="target",
                                              reason="next step")
    assert not result["ok"] and result["error_code"] == "approval_cards_hidden"
    assert "user_quote" in result["error"]
    assert session_controls.pending_outbox() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["quote", "autonomy"])
async def test_hidden_control_receipts_still_tell_a_waiting_requester(hidden_home, via):
    runner, _adapter, _source = runner_and_source()
    marked = []

    class Controls:
        @staticmethod
        def mark_outbox(request_id, flag, value=True):
            marked.append(flag)

    async def run_in_executor(fn, *args):
        return fn(*args)

    runner._run_in_executor_with_context = run_in_executor
    record = {"id": "r1", "kind": "loop", "action": "pause", "status": "applied",
              "authority": {"via": via}, "created_at": 1.0}
    await runner._settle_quiet_autonomy_notices(Controls, record)
    expected = ["target_notice_skipped"]
    if via == "autonomy":
        expected.append("requester_notification_skipped")
    assert [flag for flag in marked if flag != "outbox_done"] == expected


def test_hidden_keeps_the_goal_set_confirmation(hidden_home):
    from hermes_cli import goals
    from hermes_cli.goal_command import dispatch_goal_command
    result = dispatch_goal_command(goals.GoalManager("target"), "ship it", authorize_gate=lambda: None)
    assert "ship it" in result.output and result.kickoff
