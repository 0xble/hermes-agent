"""Bounded shutdown must never signal a recycled descendant PID."""

import signal
import time
from unittest.mock import MagicMock, patch

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(
    "exit_signal,matching_child_probes",
    [("SIGTERM", 1), ("SIGTERM", 2), ("SIGKILL", 3), ("SIGKILL", 4)],
    ids=["term-before-enqueue", "term-before-worker-signal", "kill-before-enqueue", "kill-before-worker-signal"],
)
def test_bounded_kill_validates_descendant_identity_before_signaling(
    monkeypatch, exit_signal, matching_child_probes
):
    exit_signal = getattr(signal, exit_signal)
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)
    session = ProcessSession(id="proc_reused_descendant", command="worker", task_id="t1")
    session.process = MagicMock(pid=100)
    session.host_start_time = 10
    registry._running[session.id] = session
    root_alive = True
    session.process.poll.side_effect = lambda: None if root_alive else 0
    child_probes = 0

    def start_time(pid):
        nonlocal child_probes
        if pid == 100:
            return 10
        assert pid == 200
        child_probes += 1
        # Capture the child's identity, then recycle its still-live PID either
        # before enqueueing a signal or before the worker's final identity fence.
        return 10 if child_probes <= matching_child_probes else 20

    def signal_group(pgid, sig):
        nonlocal root_alive
        if pgid == 100 and sig == exit_signal:
            root_alive = False

    # The child leaves the root tree after TERM, so refreshing before KILL must
    # retain its original fingerprint rather than rediscover the recycled PID.
    children = MagicMock(side_effect=[[MagicMock(pid=200)], []])
    monkeypatch.setattr("psutil.Process", lambda _pid: MagicMock(children=children))
    monkeypatch.setattr(registry, "_proc_alive", lambda _proc: True)
    monkeypatch.setattr(registry, "_daemon_term_grace_seconds", lambda: 0.0)
    # Exercise the real identity validator and fate classifier with consistent
    # fingerprints, without depending on whichever process owns host PID 200.
    monkeypatch.setattr(ProcessRegistry, "_is_host_pid_alive", staticmethod(lambda _pid: True))
    monkeypatch.setattr(ProcessRegistry, "_safe_host_start_time", staticmethod(start_time))
    monkeypatch.setattr("tools.process_registry.os.getpgid", lambda pid: pid)
    monkeypatch.setattr("tools.process_registry.os.getpgrp", lambda: 999)
    with patch("tools.process_registry.os.kill") as kill, \
         patch("tools.process_registry.os.killpg", side_effect=signal_group) as killpg:
        assert registry.kill_all(deadline=time.monotonic() + 2.0) == 1
    assert child_probes > matching_child_probes
    assert [call.args for call in killpg.call_args_list if call.args[0] == 200] == (
        [(200, signal.SIGTERM)] if exit_signal == signal.SIGKILL else []
    )
    assert [call.args[1] for call in killpg.call_args_list if call.args[0] == 100] == (
        [signal.SIGTERM, signal.SIGKILL] if exit_signal == signal.SIGKILL else [signal.SIGTERM]
    )
    kill.assert_not_called()
    assert session.id not in registry._running
