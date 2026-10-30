"""Slash controls own their caller and update the native idle supervision roster."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import run_checkins
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_context import bound_session_env, preserved_session_context, reset_session_vars, set_session_vars
from hermes_cli import goals
from hermes_cli.checkins import CheckinConfig, CheckinManager
from hermes_cli.commands import resolve_command
from hermes_state import SessionDB

PROOF = "pytest peer_artifact.py: 1 passed, artifact readback verified"
PEER = "hermes:other/peer"
RESOLVED = "Peer result verified and resolved."


@pytest.fixture
def parent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("goals:\n  autonomy: true\n")
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    monkeypatch.setattr("tools.async_delegation.supervisor_inventory", lambda _: [])
    monkeypatch.setattr("tools.process_registry.process_registry._running", {})
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    runner.session_store._db = db
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="7", thread_id="3")
    entry = runner.session_store.get_or_create_session(source)
    manager = goals.GoalManager(entry.session_id)
    manager.set("Deliver the verified peer artifact")
    clock = SimpleNamespace(time=lambda: 100.0)
    monkeypatch.setattr(run_checkins, "time", clock)
    monkeypatch.setattr("hermes_cli.checkins.time", clock)
    monkeypatch.setattr(CheckinConfig, "load", classmethod(lambda cls: CheckinConfig()))
    runner._run_agent = AsyncMock(side_effect=AssertionError("slash controls must not run an agent"))
    with preserved_session_context():
        # The real inbound path resets the preceding turn's context before direct slash dispatch.
        set_session_vars(session_id="prior-turn")
        reset_session_vars()
        yield SimpleNamespace(db=db, manager=manager, runner=runner, source=source, entry=entry,
                              checkin=CheckinManager(db, entry.session_id, CheckinConfig()))
    db.close()


async def command(f, text, *, busy=False):
    event = MessageEvent(text="/goal " + text, source=f.source)
    if busy:
        return await f.runner._dispatch_busy_slash_command(event, resolve_command("goal"), f.entry.session_key, f.source)
    handled, result = await f.runner._hm_dispatch_canonical_command(event, f.source, f.entry.session_key, "goal")
    assert handled is True
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True])
async def test_peer_only_slash_after_idle_scan_arms_and_exact_verified_resolution_disarms(parent, busy):
    f = parent
    sid = f.entry.session_id
    # A native poll has already seen this idle conversation with no outstanding work.
    await run_checkins.poll_checkins(f.runner)
    assert f.checkin.state is None
    assert (f.entry.session_key, sid) in f.runner._checkin_seen
    instance = f.manager.state.created_at
    assert await command(f, f'peer wait {instance} {PEER} existing-work "artifact readback"', busy=busy) == "Peer work registered."
    assert goals.load_goal(sid).pending_peers == [
        {"peer": PEER, "ref": "existing-work", "result": "artifact readback"}]
    assert f.checkin.state.due_at == 1300
    assert f.runner._checkin_watch[f.entry.session_key] == (f.source, sid)
    assert bound_session_env("HERMES_SESSION_ID", "") == ""
    # A separate reference to the SAME peer must remain outstanding.
    assert await command(f, f'peer wait {instance} {PEER} second-work "artifact readback"', busy=busy) == "Peer work registered."
    before = f.db.get_meta(f"goal:{sid}"), f.checkin.state
    assert "proof" in await command(f, f'peer resolve {instance} {PEER} existing-work "all checks passed"', busy=busy)
    assert (f.db.get_meta(f"goal:{sid}"), f.checkin.state) == before
    f.db.append_message(sid, "tool", PROOF, tool_name="terminal")
    assert await command(f, f'peer resolve {instance} {PEER} existing-work "{PROOF}"', busy=busy) == RESOLVED
    assert [row["ref"] for row in goals.load_goal(sid).pending_peers] == ["second-work"]
    assert f.checkin.state == before[1]
    assert await command(f, f'peer resolve {instance} {PEER} second-work "{PROOF}"', busy=busy) == RESOLVED
    assert goals.load_goal(sid).pending_peers == []
    assert f.checkin.state is None and not f.runner._checkin_watch
    f.runner._run_agent.assert_not_awaited()
    assert not f.db.list_meta_prefix("loop:")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["foreign", "stale", "malformed", "profile"])
async def test_slash_rejections_preserve_goal_and_unarmed_supervisor(parent, monkeypatch, case):
    f = parent
    instance = f.manager.state.created_at
    if case == "foreign":
        # A mismatched manager must not confer authority over its arbitrary target.
        monkeypatch.setattr(f.runner, "_get_goal_manager_for_event", AsyncMock(
            return_value=(f.manager, SimpleNamespace(session_id="foreign", session_key=f.entry.session_key))))
    elif case == "stale":
        instance -= 1
    elif case == "malformed":
        instance = "not-an-instance"
    else:
        monkeypatch.setattr("hermes_cli.session_controls._current_profile", lambda: "other")
        set_session_vars(session_id="prior-turn", profile="default")
    before = f.db.get_meta(f"goal:{f.entry.session_id}")
    result = await command(f, f'peer wait {instance} {PEER} existing-work "artifact readback"')
    assert result != "Peer work registered."
    assert f.db.get_meta(f"goal:{f.entry.session_id}") == before
    assert f.checkin.state is None and not getattr(f.runner, "_checkin_watch", {})
    f.runner._run_agent.assert_not_awaited()
