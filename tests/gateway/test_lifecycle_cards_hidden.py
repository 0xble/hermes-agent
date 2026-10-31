"""notices: none hides every goal and loop card from chat and hands it to the agent instead."""
import json

import pytest

import hermes_bootstrap

from agent.inline_tool_executors import _goal_receipt_text
from tests.gateway.test_goal_notices_config import runner_and_source

_HIDE_ALL = "goals:\n  notices: none\nloops:\n  notices: none\n"


@pytest.mark.asyncio
async def test_hidden_goal_and_loop_cards_reach_the_next_agent_turn(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    runner, adapter, source = runner_and_source()
    (tmp_path / "config.yaml").write_text(_HIDE_ALL, encoding="utf-8")
    token = set_hermes_home_override(tmp_path)
    try:
        await runner._send_goal_status_notice(source, "⏸ Goal paused: ship it", notice_kind="paused")
        await runner._defer_goal_status_notice_after_delivery(source, "✗ Goal failed: no route")
        await runner._defer_goal_status_notice_after_delivery(
            source, "⏳ Loop still active — stop condition judged unachievable: no canary", section="loops")
        await runner._send_goal_status_notice(source, "⏸ Loop paused — 3/3 ticks used", section="loops")
        receipt = json.dumps({"success": True, "persisted": True, "notice": "Loop set"})
        assert _goal_receipt_text("loop_set", receipt) == ""
        assert _goal_receipt_text("goal_set", receipt) == ""
        key = runner._session_key_for_source(source)
        notes = runner._consume_pending_turn_sidecar_notes(key)
    finally:
        reset_hermes_home_override(token)
    assert adapter.calls == [] and adapter.callbacks == {}
    assert [note.split("] ", 1)[1] for note in notes] == [
        "⏸ Goal paused: ship it", "✗ Goal failed: no route",
        "⏳ Loop still active — stop condition judged unachievable: no canary",
        "⏸ Loop paused — 3/3 ticks used",
    ]
    assert all("hidden from the user" in note for note in notes)
    assert runner._consume_pending_turn_sidecar_notes(key) == []


@pytest.mark.parametrize("kind,action", [("goal", "pause"), ("loop", "stop")])
def test_hidden_approval_cards_refuse_instead_of_waiting(tmp_path, monkeypatch, kind, action):
    from pathlib import Path
    from hermes_cli import goals, loops, session_controls
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_HIDE_ALL, encoding="utf-8")
    goals._DB_CACHE.clear()
    try:
        db = goals._get_session_db()
        db.create_session("target", "telegram", profile_name="default", chat_id="target",
                          chat_type="private", session_key="agent:main:telegram:dm:target")
        if kind == "goal":
            goals.GoalManager("target").set("Ship the repair")
        else:
            loops.LoopManager("target").set("watch", interval_seconds=300)
        result = session_controls.request_control(kind, action, "target", requester_sid="target",
                                                  reason="next step")
        assert not result["ok"] and result["error_code"] == "approval_cards_hidden"
        assert "user_quote" in result["error"]
        assert session_controls.pending_outbox() == []
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["quote", "autonomy"])
async def test_hidden_control_receipts_still_tell_a_waiting_requester(tmp_path, via):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    runner, _adapter, _source = runner_and_source()
    (tmp_path / "config.yaml").write_text(_HIDE_ALL, encoding="utf-8")
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
    token = set_hermes_home_override(tmp_path)
    try:
        await runner._settle_quiet_autonomy_notices(Controls, record)
    finally:
        reset_hermes_home_override(token)
    expected = ["target_notice_skipped"]
    if via == "autonomy":
        expected.append("requester_notification_skipped")
    assert [flag for flag in marked if flag != "outbox_done"] == expected
