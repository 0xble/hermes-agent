"""Goal visibility is independent of goal persistence and continuation."""
from types import SimpleNamespace

import pytest
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.goal_command import dispatch_goal_command
from hermes_cli import goals
from tests.gateway.test_goal_status_notice import FakeAdapter


def runner_and_source():
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = FakeAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)
    source = SessionSource(platform=Platform.DISCORD, chat_id="channel", thread_id="thread", user_id="user")
    return runner, adapter, source


@pytest.mark.asyncio
async def test_profile_notice_policy_controls_sends_and_preserves_loops(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    runner, adapter, source = runner_and_source()
    quiet, normal = tmp_path / "quiet", tmp_path / "normal"
    for home, config in [(quiet, "goals:\n  notices: false\n"), (normal, "{}\n")]:
        home.mkdir()
        (home / "config.yaml").write_text(config, encoding="utf-8")
    for home in [quiet, normal, quiet]:
        token = set_hermes_home_override(home)
        try:
            await runner._send_goal_status_notice(source, "Goal verdict")
            await runner._defer_goal_status_notice_after_delivery(source, "Goal deferred")
            if home == quiet:
                assert adapter.callbacks == {}
            else:
                _, callback = adapter.callbacks.popitem()[1]
                await callback()
            if home == quiet:
                await runner._send_goal_status_notice(source, "Loop completed", section="loops")
        finally:
            reset_hermes_home_override(token)
    assert [call["content"] for call in adapter.calls] == [
        "Loop completed", "Goal verdict", "Goal deferred", "Loop completed",
    ]


def test_quiet_goal_set_keeps_kickoff_and_explicit_queries(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("goals:\n  notices: false\n", encoding="utf-8")
    token = set_hermes_home_override(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    try:
        goals._get_session_db()
        mgr = goals.GoalManager("quiet-notice-test")
        result = dispatch_goal_command(mgr, "ship it\nverify: tests pass", authorize_gate=lambda: None)
        assert result.output == ""
        assert result.kickoff and result.prompt
        assert goals.load_goal(mgr.session_id).status == "active"
        assert "ship it" in dispatch_goal_command(mgr, "status", authorize_gate=lambda: None).output
        assert "tests pass" in dispatch_goal_command(mgr, "show", authorize_gate=lambda: None).output
        error = dispatch_goal_command(mgr, "wait invalid", authorize_gate=lambda: None)
        assert error.error and error.output
    finally:
        for key, db in list(goals._DB_CACHE.items()):
            if str(home) in str(key):
                db.close()
                del goals._DB_CACHE[key]
        reset_hermes_home_override(token)