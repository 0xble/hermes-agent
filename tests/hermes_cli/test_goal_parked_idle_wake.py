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


def test_kill_all_process_lifts_barrier_with_an_outcome_only_note(hermes_home):
    """A legacy kill_all receipt proves an incomplete result, not who stopped it."""
    proc = "proc_d2196a6c1051"
    _write_receipt(hermes_home, proc, exit_code=-15, completion_reason="killed", termination_source="kill_all")
    mgr = _park_on_session("s-killed", proc)

    prompt = mgr.lifted_barrier_prompt()

    assert prompt is not None and "ship the nightly check" in prompt
    assert f"{proc} stopped before it finished (exit -15)" in prompt
    for cause in ("gateway_shutdown", "shutdown", "restart", "gateway"):
        assert cause not in prompt.lower()
    assert "verify the real state" in prompt
    # Pure: the barrier stays until the caller's continuation was admitted.
    assert goals.load_goal("s-killed").waiting_on_session == proc


def test_untracked_process_reports_unknown_outcome(hermes_home):
    mgr = _park_on_session("s-unknown", "proc_gone0000000")
    prompt = mgr.lifted_barrier_prompt()
    assert prompt is not None
    assert "no longer tracked" in prompt and "do not assume it succeeded" in prompt
    assert "output or artifacts" in prompt and "before rerunning" in prompt
    for cause in ("gateway_shutdown", "shutdown", "restart", "gateway"):
        assert cause not in prompt.lower()


def test_barrier_that_still_holds_yields_no_prompt(hermes_home, monkeypatch):
    mgr = _park_on_session("s-live", "proc_live0000000")
    monkeypatch.setattr(goals, "_session_waiting", lambda sid: True)
    assert mgr.lifted_barrier_prompt() is None


def test_age_cap_rearms_a_still_running_wait(hermes_home, monkeypatch):
    mgr = _park_on_session("s-cap", "proc_slow000000")
    monkeypatch.setattr(goals, "_session_waiting", lambda sid: True)
    mgr.state.waiting_since = time.time() - goals._MAX_BARRIER_WAIT_S - 5
    mgr.state.barrier_recheck_at = 0.0
    mgr._save()
    monkeypatch.setattr(goals, "_process_outcome", lambda sid: {"running": True})
    mgr.rearm_live_barrier()
    prompt = mgr.lifted_barrier_prompt()
    assert prompt is None
    assert mgr.is_waiting() is True
    assert mgr.state is not None
    assert mgr.state.barrier_recheck_at > time.time()
    assert mgr.state.waiting_until == 0.0
    assert mgr.state.barrier_rearms == 1


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


def test_clear_lifted_wait_respects_a_newer_repark_or_pause(hermes_home, monkeypatch):
    """A writer that lands between the idle surface's snapshot and its persistence must win: the
    compare and the write run in one transaction, so a stale snapshot can never be saved over it."""
    mgr = _park_on_session("s-race", "proc_first000000")
    since = mgr.state.waiting_since
    stale = goals.load_goal("s-race")  # the snapshot a non-atomic clear would have written back

    newer = goals.load_goal("s-race")
    newer.waiting_since = since + 10
    newer.waiting_on_session = "proc_second00000"
    goals.save_goal("s-race", newer)  # the resumed turn re-parks after the snapshot was taken

    assert stale.waiting_since == since
    assert mgr.clear_lifted_wait(since) is False
    row = goals.load_goal("s-race")
    assert row.waiting_on_session == "proc_second00000" and row.waiting_since == since + 10
    assert mgr.state.waiting_on_session == "proc_second00000"  # manager adopts the durable row

    paused = goals.load_goal("s-race")
    paused.status = "paused"
    goals.save_goal("s-race", paused)
    assert mgr.clear_lifted_wait(since + 10) is False  # a concurrent pause is never reactivated
    assert goals.load_goal("s-race").status == "paused"


def test_competing_writer_cannot_land_inside_the_clear(hermes_home, monkeypatch):
    """Interleave a second writer exactly between the compare and the write: the clear holds the
    SQLite write lock (BEGIN IMMEDIATE), so the competitor is refused and cannot be overwritten."""
    import sqlite3

    mgr = _park_on_session("s-lock", "proc_lock0000000")
    since = mgr.state.waiting_since
    db_path = hermes_home / "state.db"
    outcomes = []
    real_clear = goals.GoalState.clear_wait

    def clear_with_competitor(self, *args, **kwargs):
        other = sqlite3.connect(str(db_path), timeout=0)
        try:
            other.execute("UPDATE state_meta SET value = value WHERE key = ?", (goals._meta_key("s-lock"),))
            other.commit()
            outcomes.append("landed")
        except sqlite3.OperationalError as exc:
            outcomes.append(str(exc))
        finally:
            other.close()
        return real_clear(self, *args, **kwargs)

    monkeypatch.setattr(goals.GoalState, "clear_wait", clear_with_competitor)
    assert mgr.clear_lifted_wait(since) is True
    assert outcomes and "locked" in outcomes[0]
    assert goals.load_goal("s-lock").waiting_on_session is None


def test_clear_is_one_write_transaction(hermes_home):
    mgr = _park_on_session("s-txn", "proc_txn00000000")
    db = goals._get_session_db()
    calls = []
    real = db._execute_write

    def spy(fn, *a, **k):
        calls.append(fn)
        return real(fn, *a, **k)

    db._execute_write = spy
    try:
        assert mgr.clear_lifted_wait(mgr.state.waiting_since) is True
    finally:
        db._execute_write = real
    assert len(calls) == 1
    assert goals.load_goal("s-txn").waiting_on_session is None


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
