"""Gateway user turns survive a corrupt or transiently unreadable goal row.

Regression for the PR #551 review: ``_revive_blocked_goal_for_user_turn`` runs on every admitted
user turn, so a goal row that failed to load raised out of it and errored the turn.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_state import SessionDB


@pytest.fixture
def env(tmp_path, monkeypatch):
    from hermes_cli import goals

    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1", chat_type="dm")
    entry = SimpleNamespace(session_id="session-a", last_prompt_tokens=100)
    monkeypatch.setattr(runner, "_session_entry_for_manager", AsyncMock(return_value=entry))
    monkeypatch.setattr(runner, "_send_goal_status_notice", AsyncMock())
    yield runner, source, entry, db
    db.close()


@pytest.mark.asyncio
async def test_user_turn_goal_hooks_complete_over_a_corrupt_row(env, monkeypatch):
    from hermes_cli import goals

    runner, source, entry, db = env
    db.set_meta("goal:session-a", "{corrupt")
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: pytest.fail("an unreadable goal is never judged"))
    event = MessageEvent(text="hello", source=source)
    assert runner._is_user_turn_event(event)

    assert await runner._revive_blocked_goal_for_user_turn(entry, source, event) is None
    await runner._post_turn_goal_continuation(session_entry=entry, source=source, final_response="done")
    mgr, _entry = await runner._get_goal_manager_for_event(event)
    assert mgr.unreadable
    assert runner._apply_subgoal_command(mgr, "add tests")
    runner._send_goal_status_notice.assert_not_awaited()
    assert db.get_meta("goal:session-a") == "{corrupt"


@pytest.mark.asyncio
async def test_transient_goal_read_failure_does_not_error_the_user_turn(env, monkeypatch):
    from hermes_cli import goals

    runner, source, entry, db = env
    goals.save_goal("session-a", goals.GoalState(
        goal="probe", status="paused", last_verdict="blocked",
        paused_reason=goals._BLOCKED_PAUSE_PREFIX + "needs input"))
    read, failed = db.get_meta, []

    def flaky(key, *args, **kwargs):
        if key == "goal:session-a" and not failed:
            failed.append(key)
            raise RuntimeError("database is locked")
        return read(key, *args, **kwargs)

    monkeypatch.setattr(db, "get_meta", flaky)
    event = MessageEvent(text="here is the input", source=source)

    assert await runner._revive_blocked_goal_for_user_turn(entry, source, event) is None
    assert failed
    assert goals.load_goal("session-a").status == "paused"
