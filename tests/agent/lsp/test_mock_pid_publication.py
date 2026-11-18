"""The mock server's PID path is a readiness signal, not a work-in-progress file."""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from unittest.mock import Mock

from tests.agent.lsp import _mock_lsp_server as mock_server


def test_slow_tree_child_publishes_complete_pid_after_signal_setup(tmp_path, monkeypatch):
    """Observe the real empty-file interval before write_text writes its first byte."""
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("MOCK_LSP_SCRIPT", "slow_tree")
    monkeypatch.setenv("MOCK_LSP_CHILD_PID", str(pid_file))
    spawn = Mock()
    signals = Mock()
    monkeypatch.setattr(mock_server.subprocess, "Popen", spawn)
    monkeypatch.setattr(mock_server, "read_message", lambda: None)
    monkeypatch.setattr(signal, "signal", signals)
    assert mock_server.main() == 0
    spawn.assert_called_once()
    child_command = spawn.call_args.args[0]
    signals.reset_mock()  # Only the child's handler makes its PID ready.

    observations = []
    real_open = Path.open

    def observe_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if "w" in mode:
            # Native open has created/truncated the file, but the producer has not
            # written yet. Run the readiness observer at exactly that boundary.
            signals.assert_called_once_with(signal.SIGTERM, signal.SIG_IGN)
            observations.append(pid_file.read_bytes() if pid_file.exists() else None)
        return handle

    monkeypatch.setattr(Path, "open", observe_open)
    monkeypatch.setattr(time, "sleep", Mock())  # Publication, not child lifetime, is under test.
    # Execute the actual command selected by slow_tree, not a copied producer.
    # Signals and lifetime are stubbed; all file creation/writes/replacement are real.
    assert child_command[1] == "-c"
    exec(child_command[2], {"__name__": "__main__"})

    assert observations == [None], f"PID readiness exposed incomplete contents: {observations!r}"
    assert int(pid_file.read_text(encoding="utf-8")) == os.getpid()
