"""Shared isolated store and clock for one-shot lifecycle tests."""
from datetime import UTC, datetime

import pytest

from cron import executions, jobs, scheduler, scheduler_tick

NOW = datetime(2026, 10, 29, 17, 21, 28, tzinfo=UTC)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "_hermes_now", lambda: NOW)
    monkeypatch.setattr(executions, "_hermes_now", lambda: NOW)
    monkeypatch.setattr(scheduler, "_should_yield_tick_to_fresh_gateway", lambda: None)
    monkeypatch.setattr(scheduler_tick, "_pre_dispatch_housekeeping", lambda *args: None)
    monkeypatch.setattr(scheduler, "_maybe_run_worktree_maintenance", lambda: None)
    monkeypatch.setattr(scheduler, "_sweep_mcp_orphans", lambda: None)
    jobs.ensure_dirs()
    return tmp_path / "cron"
