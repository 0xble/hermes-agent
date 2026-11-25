"""Regression test for ack/exit race in cron scheduler external worker launch."""

from __future__ import annotations

import json

from pathlib import Path as StdPath

import pytest

from tools.process_registry import GatewayChildDispatch

from cron import scheduler


def test_launch_external_worker_accepts_ack_published_just_before_exit_race(tmp_path, monkeypatch):
    """Regression for ack/exit race: worker publishes .ready then exits (poll=0) between
    the loop's exists() check (False) and poll(). The re-check after poll must accept the
    ack via the factored helper and proceed to acknowledged path (no RuntimeError, .ready consumed).
    Deterministic via monkeypatched exists sequence + pre-written ack + immediate-exit FakeProcess.
    """
    job = {"id": "job-race", "execution_id": "exec-race", "prompt": "work"}
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        "tools.process_registry.restart_safe_gateway_child_argv",
        lambda command, **_kw: GatewayChildDispatch("detached", ["detached", *command]),
    )
    monkeypatch.setattr(
        scheduler,
        "mark_execution_handoff_pending",
        lambda _execution_id: {"id": "exec-race", "handoff_pending": 1},
    )
    handoff_dir = tmp_path / "cron" / "external-workers"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    ack_path = handoff_dir / "exec-race.ready"
    # Pre-publish ack (fast worker case) with valid content for this execution_id
    ack_path.write_text(
        json.dumps(
            {
                "pid": 12345,
                "execution_id": "exec-race",
                "hard_wall_deadline": 1234567890.0,
            }
        ),
        encoding="utf-8",
    )

    class FakeProcess:
        pid = 999
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return 0

    process = FakeProcess()
    monkeypatch.setattr(scheduler.subprocess, "Popen", lambda *_a, **_k: process)

    # Monkeypatch exists sequence to simulate the race: first check misses (False),
    # re-check after poll sees it (True). Only affects this ack_path.
    exists_calls = []
    original_exists = StdPath.exists

    def fake_exists(self):
        exists_calls.append(str(self))
        if str(self).endswith("exec-race.ready"):
            if len([c for c in exists_calls if c.endswith("exec-race.ready")]) == 1:
                return False  # race miss on first check
            return True
        return original_exists(self)

    monkeypatch.setattr(StdPath, "exists", fake_exists)

    # Keep loop from sleeping/expiring
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(scheduler.time, "sleep", lambda _s: None)
    monkeypatch.setattr(scheduler, "_running_worker_pids", {})

    def read_execution(_execution_id):
        return {"id": "exec-race", "status": "completed"}

    monkeypatch.setattr(scheduler, "get_execution", read_execution, raising=False)

    # Must not raise; must take the re-check path and succeed
    result = scheduler._launch_external_cron_worker(job)
    assert result is True
    # Helper consumed (unlinked) the .ready exactly as normal ack path (use original_exists because fake masks real state)
    assert not original_exists(ack_path)
    # At least one re-check happened (the race simulation exercised)
    assert any(c.endswith("exec-race.ready") for c in exists_calls)
