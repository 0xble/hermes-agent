"""loops.autonomy lets the agent run its loop's lifecycle; user pauses, clear and floors stay."""

import pytest

# User-message persistence reaches the runtime bootstrap. Load it before the
# per-test real-home guard, as the application entry point does.
import hermes_bootstrap

from hermes_cli import goals, loops, session_controls


@pytest.fixture
def home(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "loops:\n  autonomy: true\n  max_ticks: 3\n", encoding="utf-8")
    goals._DB_CACHE.clear()
    db = goals._get_session_db()
    db.create_session("loop", "telegram", profile_name="default", chat_id="loop",
                      chat_type="private", session_key="agent:main:telegram:dm:loop")
    yield tmp_path
    goals._DB_CACHE.clear()


def _loop(sid="loop"):
    manager = loops.LoopManager(sid)
    manager.set("watch the deploy", interval_seconds=300, times=5, until="deploy green")
    return manager


def _control(action, sid="loop", **kwargs):
    return session_controls.apply_control("loop", action, sid, requester_sid=sid,
                                          reason="next step", **kwargs)


def test_agent_runs_its_loop_lifecycle_without_quotes_inside_the_floors(home):
    manager = _loop()
    for change in ({"prompt": "watch the canary"}, {"until": "canary green"},
                   {"interval_seconds": 1}, {"times": 0}, {"self_paced": True}):
        result = loops.LoopManager("loop").revise(reason="better route", **change)
        assert result["ok"], (change, result)
        assert result["revision"]["authority"] == "autonomy"
    state = loops.load_loop("loop")
    assert (state.prompt, state.until, state.times, state.mode) == (
        "watch the canary", "canary green", 0, "self_paced")
    assert state.current_delay == loops.self_paced_floor_seconds()
    assert [r["after"].get("interval_seconds") for r in state.revisions][2] == loops.min_interval_seconds()

    replaced = loops.LoopManager("loop").replace(prompt="watch the rollout", interval_seconds=1, reason="new target",
                                                 user_quote="")
    assert replaced["ok"] and replaced["revision"]["authority"] == "autonomy"
    state = loops.load_loop("loop")
    assert state.interval_seconds == loops.min_interval_seconds() and state.max_ticks == 3

    requested = session_controls.request_control("loop", "pause", "loop", requester_sid="loop", reason="next step")
    assert requested["ok"] and requested["status"] == "applied", requested
    assert loops.load_loop("loop").paused_reason == loops.AGENT_PAUSE_PREFIX + "next step"
    for action, status in (("resume", "active"), ("pause", "paused"), ("stop", "done")):
        result = _control(action)
        assert result["ok"] and result["status"] == "applied", (action, result)
        assert loops.load_loop("loop").status == status
    finished = loops.load_loop("loop")
    assert finished.last_stop_reason == "next step" and finished.prompt == "watch the rollout"
    assert not _control("resume")["ok"]
    assert loops.load_loop("loop").status == "done"
    del manager


@pytest.mark.parametrize("pause", ["slash", "quote", "ctrl_c", "tick_budget"])
def test_agent_never_lifts_a_pause_it_did_not_make(home, monkeypatch, pause):
    manager = _loop()
    if pause == "slash":
        loops.dispatch_loop_command(manager, "pause")
    elif pause == "quote":
        goals._get_session_db().append_message("loop", "user", "Pause the loop.")
        assert _control("pause", user_quote="Pause the loop.")["ok"]
    elif pause == "ctrl_c":
        manager.pause(reason="user-interrupted (Ctrl+C)")
    else:
        monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "not yet", False, None, False))
        for _ in range(3):
            manager.state.next_due_at = 0
            manager._save()
            assert manager.fire_tick()
            manager.complete_tick("deploy still rolling out")
        assert loops.load_loop("loop").paused_reason.startswith("tick budget exhausted")
    before = loops.load_loop("loop")
    assert before.status == "paused"
    assert not _control("pause")["ok"]  # re-pausing cannot relabel it as the agent's own
    result = _control("resume")
    assert not result["ok"] and result["error_code"] == "user_paused", result
    assert loops.load_loop("loop").status == "paused"
    assert loops.load_loop("loop").paused_reason == before.paused_reason


def test_default_off_keeps_quote_authority(home):
    home.joinpath("config.yaml").write_text("loops:\n  autonomy: false\n", encoding="utf-8")
    manager = _loop()
    assert manager.revise(reason="reword", prompt="new")["error_code"] == "user_authority_required"
    assert manager.revise(reason="faster", interval_seconds=60)["error_code"] == "user_authority_required"
    replaced = manager.replace(prompt="other", reason="new target", user_quote="")
    assert replaced["error_code"] == "user_authority_required"
    assert "authority" not in manager.revise(reason="slower", interval_seconds=600)["revision"]
