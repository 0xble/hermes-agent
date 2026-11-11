"""Kanban reaps only its workers and preserves their exit and signal outcomes."""

from __future__ import annotations

import signal
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


@pytest.mark.parametrize("code, kind", [(0, "clean_exit"), (7, "nonzero_exit"),
                                      (kb.KANBAN_RATE_LIMIT_EXIT_CODE, "rate_limited")])
def test_reaper_collects_only_registered_workers(tmp_path, monkeypatch, code, kind):
    """Keep worker outcomes without stealing a terminal/provider child's exit status."""
    from tests.waiting import process_alive, wait_until
    from tests.hermes_cli.test_kanban_worker_spawn_toolsets import _make_task
    monkeypatch.setattr(kbd, "_live_worker_procs", {})
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    monkeypatch.setattr(kbd, "_worker_argv", lambda *_: [sys.executable, "-c", f"raise SystemExit({code})"])
    pid = kbd._default_spawn(_make_task(kb, assignee="default"), str(tmp_path))
    proc = kbd._live_worker_procs[pid]
    foreign = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
    try:
        wait_until(lambda: not process_alive(proc.pid), what="Kanban worker exited")
        wait_until(lambda: not process_alive(foreign.pid), what="foreign child exited")
        reaped = kbd.reap_worker_zombies()
        foreign_exit = foreign.wait(timeout=10)
        assert (reaped, foreign_exit) == ([proc.pid], 7)
        assert kbd._classify_worker_exit(proc.pid) == (kind, code)
        assert not kbd._live_worker_procs
        assert kbd.reap_worker_zombies() == []
    finally:
        proc.wait(timeout=10)
        foreign.wait(timeout=10)


@pytest.mark.platforms("posix")
def test_reaper_preserves_worker_signal(monkeypatch):
    monkeypatch.setattr(kbd, "_live_worker_procs", {})
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    proc = subprocess.Popen([sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"])
    assert proc.wait(timeout=10) == -signal.SIGTERM
    kbd._live_worker_procs[proc.pid] = proc
    assert kbd.reap_worker_zombies() == [proc.pid]
    assert kbd._classify_worker_exit(proc.pid) == ("signaled", signal.SIGTERM)
