"""Bootstrap cleanup works without psutil or a full-featured ps."""
from __future__ import annotations

import signal
import subprocess

import pytest


@pytest.mark.platforms("posix")
def test_busybox_ps_failure_uses_proc_descendants(tmp_path, monkeypatch):
    import pm.client_process as processes

    for pid, parent, name in [(101, 1, "worker"), (202, 101, "uv (sync)"),
                              (303, 202, "python) child"), (404, 1, "unrelated")]:
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / "stat").write_text(f"{pid} ({name}) S {parent} 0 0 0\n")
    (tmp_path / "505").mkdir()  # A process disappears during the snapshot.

    def ps(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="ps: unrecognized option: a")

    monkeypatch.setattr(processes.subprocess, "run", ps)
    monkeypatch.setattr(processes, "Path", lambda path: tmp_path, raising=False)
    killed = []
    monkeypatch.setattr(processes.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    processes._kill_bootstrap_tree(101)
    assert killed == [(303, signal.SIGKILL), (202, signal.SIGKILL), (101, signal.SIGKILL)]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("group", [101, 99])
def test_no_snapshot_signals_only_worker_owned_group(monkeypatch, group):
    import pm.client_process as processes

    monkeypatch.setattr(processes, "_bootstrap_parents", lambda: None, raising=False)
    monkeypatch.setattr(processes.os, "getpgid", lambda pid: group)
    killed, groups = [], []
    monkeypatch.setattr(processes.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(processes.os, "killpg", lambda pid, sig: groups.append(pid))
    processes._kill_bootstrap_tree(101)
    assert groups == ([101] if group == 101 else [])
    assert killed == ([] if group == 101 else [101])
