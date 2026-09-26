"""TUI/Desktop/dashboard sessions resume a parked /goal once its wait barrier lifted, without a turn.

Same wedge as the gateway: the post-turn judge is the only other re-check, so a process killed by a
restart (or an elapsed timed wait) parked the goal until the user typed. The session-owner poller
drives it exactly like /loop and /heartbeat ticks.
"""

from __future__ import annotations

import importlib
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


@pytest.fixture()
def server(hermes_home):
    with patch.dict("sys.modules", {"hermes_cli.env_loader": MagicMock(), "hermes_cli.banner": MagicMock()}):
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()


@pytest.fixture()
def session(server):
    sid, key = "sid-goal-wake", "tui-goal-wake-1"
    s = {"session_key": key, "history": [], "history_lock": threading.Lock(), "history_version": 0,
         "running": False, "attached_images": [], "cols": 120, "agent": MagicMock()}
    server._sessions[sid] = s
    return sid, key, s


def _park_elapsed(key: str):
    from hermes_cli.goals import GoalManager

    mgr = GoalManager(key)
    mgr.set("finish the migration")
    mgr.wait_for_seconds(60, reason="cooldown")
    mgr.state.waiting_until = time.time() - 1
    mgr._save()
    return mgr


def test_poller_resumes_a_lifted_goal_wait_once(server, session):
    sid, key, s = session
    _park_elapsed(key)
    dispatched: list[str] = []

    def submit(rid, sid_, session_, text, **kw):
        dispatched.append(text)
        return True

    with patch.object(server, "_run_prompt_submit", submit), patch.object(server, "_emit"):
        server._maybe_resume_tui_parked_goal(sid, s)
        server._maybe_resume_tui_parked_goal(sid, s)  # barrier cleared: no second continuation

    from hermes_cli.goals import load_goal

    assert len(dispatched) == 1 and "finish the migration" in dispatched[0]
    assert load_goal(key).waiting_until == 0.0


@pytest.mark.parametrize("why", ["busy", "refused"])
def test_poller_keeps_the_barrier_when_no_turn_started(server, session, why):
    sid, key, s = session
    _park_elapsed(key)
    s["running"] = why == "busy"

    def submit(*a, **k):
        with s["history_lock"]:
            s["running"] = False
        return False

    with patch.object(server, "_run_prompt_submit", submit), patch.object(server, "_emit"):
        server._maybe_resume_tui_parked_goal(sid, s)

    from hermes_cli.goals import load_goal

    assert load_goal(key).waiting_until != 0.0  # retried on the next poll
    assert s["running"] is (why == "busy")
