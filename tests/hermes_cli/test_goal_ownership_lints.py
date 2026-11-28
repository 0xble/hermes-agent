from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


def test_ownership_set_requires_due_and_acceptance(tmp_path, monkeypatch):
    from hermes_cli.goals import GoalManager, GoalContract
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    home = tmp_path / "home"
    home.mkdir()
    token = set_hermes_home_override(str(home))
    try:
        manager = GoalManager("lint-set")
        with pytest.raises(ValueError, match="due"):
            manager.set("Ship the result", ownership=True, contract=GoalContract(verification="check"))
        plan = tmp_path / "plan.md"
        plan.write_text("plan")
        state = manager.set("Ship the result", ownership=True, due="2030-01-01",
                             done_when="the canary is green", plan_path=str(plan))
        assert state.ownership and state.due == "2030-01-01"
    finally:
        reset_hermes_home_override(token)


def test_ownership_paths_and_approval_wording_are_actionable(tmp_path):
    from hermes_cli.goal_lints import enforce_ownership, GoalLintError
    with pytest.raises(GoalLintError, match="outside ~/.hermes/cache"):
        enforce_ownership(due="2030-01-01", done_when="check", plan_path="~/.hermes/cache/plan.md", ownership=True)
    with pytest.raises(GoalLintError, match="quote Brian"):
        enforce_ownership(due="2030-01-01", done_when="check", constraints="Wait for Brian approval", ownership=True)
    with pytest.raises(GoalLintError, match="does not exist"):
        enforce_ownership(due="2030-01-01", done_when="check", log_path=str(tmp_path / "missing"), ownership=True)


def test_due_relaxation_requires_quote_and_records_baseline(tmp_path, monkeypatch):
    from hermes_cli.goals import GoalManager
    from hermes_cli.goals_autonomy import goal_autonomy_enabled
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("goals:\n  autonomy: true\n")
    token = set_hermes_home_override(str(home))
    try:
        manager = GoalManager("lint-revise")
        manager.set("Ship the result", ownership=True, due="2030-01-01", done_when="canary green")
        refused = manager.revise(reason="slip", due="2030-02-01", ownership=True)
        assert refused["error_code"] == "due_relaxation_quote_required"
        changed = manager.revise(reason="Brian approved the date", due="2030-02-01", user_quote="Brian approved the date", ownership=True)
        assert changed["ok"]
        state = manager.state
        assert state.baseline[-1]["previous_due"] == "2030-01-01"
    finally:
        reset_hermes_home_override(token)


def test_hygiene_is_read_only_and_uses_requested_counts(tmp_path):
    from scripts.goal_hygiene import report
    db_path = tmp_path / "state.db"
    db = sqlite3.connect(db_path)
    db.execute("create table state_meta(key text primary key, value text)")
    db.execute("insert into state_meta values (?, ?)", ("goal:a", json.dumps({"status": "active"})))
    db.commit(); db.close()
    before = db_path.stat().st_mtime_ns
    counts = report(str(db_path))
    assert counts["active_goals"] == 1
    assert counts["goals_missing_due_or_done_when"] == 1
    assert db_path.stat().st_mtime_ns == before
