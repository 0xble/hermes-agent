"""Idle wake for parked /goal waits whose completion notice never arrives.

A goal parked on a background process re-evaluates only after a turn, normally the process's
completion notice. A gateway restart kills the process (``kill_all``) and its in-memory notice dies
with the old process, so nothing ever re-judges the goal. These tests cover the shared goal-side
contract the CLI, gateway and TUI idle surfaces use.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import goals


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(home))
    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield home
    reset_hermes_home_override(token)
    goals._DB_CACHE.clear()


def _park_on_session(sid: str, proc: str, *, goal: str = "ship the nightly check") -> goals.GoalManager:
    mgr = goals.GoalManager(sid)
    mgr.set(goal)
    mgr.wait_on_session(proc, reason="nightly verification running")
    return mgr


def _write_receipt(home: Path, proc: str, **fields) -> None:
    directory = home / "logs" / "process-results"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{proc}.json").write_text(json.dumps({"id": proc, **fields}), encoding="utf-8")


def test_restart_killed_process_lifts_barrier_with_a_factual_note(hermes_home):
    """The live case: parked on a process a restart's kill_all killed. The previous gateway's
    registry is gone, so only the durable receipt knows what happened."""
    proc = "proc_d2196a6c1051"
    _write_receipt(hermes_home, proc, exit_code=-15, completion_reason="killed", termination_source="kill_all")
    mgr = _park_on_session("s-killed", proc)

    prompt = mgr.lifted_barrier_prompt()

    assert prompt is not None and "ship the nightly check" in prompt
    assert f"{proc} was killed by a gateway restart or shutdown" in prompt
    assert "verify the real state" in prompt
    # Pure: the barrier stays until the caller's continuation was admitted.
    assert goals.load_goal("s-killed").waiting_on_session == proc


def test_untracked_process_reports_unknown_outcome(hermes_home):
    mgr = _park_on_session("s-unknown", "proc_gone0000000")
    prompt = mgr.lifted_barrier_prompt()
    assert prompt is not None
    assert "no longer tracked" in prompt and "do not assume it succeeded" in prompt


def test_barrier_that_still_holds_yields_no_prompt(hermes_home, monkeypatch):
    mgr = _park_on_session("s-live", "proc_live0000000")
    monkeypatch.setattr(goals, "_session_waiting", lambda sid: True)
    assert mgr.lifted_barrier_prompt() is None


def test_age_cap_lifts_a_still_running_wait(hermes_home, monkeypatch):
    mgr = _park_on_session("s-cap", "proc_slow0000000")
    monkeypatch.setattr(goals, "_session_waiting", lambda sid: True)
    mgr.state.waiting_since = time.time() - goals._MAX_BARRIER_WAIT_S - 5
    mgr._save()
    monkeypatch.setattr(goals, "_process_outcome", lambda sid: {"running": True})
    prompt = mgr.lifted_barrier_prompt()
    assert prompt is not None and "still running" in prompt


def test_elapsed_timed_wait_lifts_without_a_note(hermes_home):
    mgr = goals.GoalManager("s-timer")
    mgr.set("poll again later")
    mgr.wait_for_seconds(60, reason="cooldown")
    assert mgr.lifted_barrier_prompt() is None
    mgr.state.waiting_until = time.time() - 1
    mgr._save()
    prompt = mgr.lifted_barrier_prompt()
    assert prompt == mgr.next_continuation_prompt()


def test_fresh_notify_completion_defers_to_its_own_turn(hermes_home, monkeypatch):
    """A process that exited normally in THIS gateway still has its completion notice queued; the idle
    wake must not race a duplicate continuation ahead of it."""
    from tools.process_registry import process_registry

    proc = "proc_fresh000000"
    finished = SimpleNamespace(id=proc, exited=True, exit_code=0, completion_reason="exited",
                               termination_source="", notify_on_complete=True, exited_at=time.time())
    monkeypatch.setitem(process_registry._finished, proc, finished)
    mgr = _park_on_session("s-fresh", proc)
    assert mgr.lifted_barrier_prompt() is None
    finished.exited_at = time.time() - goals._COMPLETION_NOTICE_GRACE_S - 1
    prompt = mgr.lifted_barrier_prompt()
    assert prompt is not None and "finished (exited, exit 0)" in prompt


def test_clear_lifted_wait_spares_a_newer_barrier(hermes_home):
    """The resumed turn can finish and re-park before the idle surface clears; the clear must match the
    wait it resumed, never erase the new one."""
    mgr = _park_on_session("s-repark", "proc_first000000")
    since = mgr.state.waiting_since
    other = goals.GoalManager("s-repark")
    other.state.waiting_since = since + 10
    other.state.waiting_on_session = "proc_second00000"
    other._save()

    assert mgr.clear_lifted_wait(since) is False
    assert goals.load_goal("s-repark").waiting_on_session == "proc_second00000"
    assert mgr.clear_lifted_wait(since + 10) is True
    assert goals.load_goal("s-repark").waiting_on_session is None


def test_list_parked_goals_and_store_gate(hermes_home):
    _park_on_session("s-parked", "proc_a00000000000")
    goals.GoalManager("s-active").set("unparked goal")
    done = goals.GoalManager("s-done")
    done.set("finished")
    done.state.status = "done"
    done._save()

    assert [sid for sid, _ in goals.list_parked_goals()] == ["s-parked"]
    assert goals.store_has_parked_goal(goals._get_session_db()) is True
    goals.GoalManager("s-parked").stop_waiting()
    assert goals.list_parked_goals() == []
    assert goals.store_has_parked_goal(goals._get_session_db()) is False
