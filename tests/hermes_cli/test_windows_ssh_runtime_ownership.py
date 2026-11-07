"""Boot-selecting runtime ownership uses the actual argv parser on native processes."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import psutil
import pytest

from hermes_cli.windows_ssh_runtime import process_state
from tests.waiting import wait_until


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("argv_initially_unavailable", [False, True])
def test_isolated_bootstrap_is_owned_without_accepting_foreign_source(tmp_path, monkeypatch, argv_initially_unavailable):
    package = tmp_path / "hermes_cli"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "main.py").write_text("import time; time.sleep(30)\n")
    source = ("import os, sys, runpy; "
              f"sys.path.insert(0, {str(tmp_path)!r}); "
              "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)")
    nonce = "a" * 16
    env = {"PATH": os.defpath, "HOME": str(tmp_path)}
    child = subprocess.Popen([sys.executable, "-I", "-c", source, "serve", "--isolated", "--ssh-owner-nonce", nonce], env=env)
    try:
        created = int(psutil.Process(child.pid).create_time() * 1_000_000_000)
        launcher = str(tmp_path / "hermes.exe")
        if argv_initially_unavailable:
            cmdline = psutil.Process.cmdline
            unavailable = True

            def initially_unavailable(process):
                nonlocal unavailable
                if process.pid == child.pid and unavailable:
                    unavailable = False
                    return []
                return cmdline(process)

            monkeypatch.setattr(psutil.Process, "cmdline", initially_unavailable)

        # A spawned process can be alive before its argv is observable. Retry only
        # that indeterminate observation, never a determinate ownership rejection.
        def observable_state():
            state = process_state(child.pid, created, launcher, nonce)
            return state if state["reason"] != "argv-unavailable" else None

        state = wait_until(observable_state, what="the bootstrap child's argv to become observable")
        assert state is not None
        assert state["alive"] and state["owned"], state
        assert not process_state(child.pid, created, launcher, "b" * 16)["owned"]
        assert not process_state(child.pid, created + 1, launcher, nonce)["owned"]
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_foreign_inline_source_carrying_a_module_name_is_not_ownership(monkeypatch, tmp_path):
    argv = [sys.executable, "-c", "print('hermes_cli.main')", "serve", "--isolated", "--ssh-owner-nonce", "a" * 16]

    class Process:
        def create_time(self):
            return 1_700_000_000.0
        def cmdline(self):
            return argv
        def is_running(self):
            return True

    monkeypatch.setattr(psutil, "Process", lambda _pid: Process())
    state = process_state(123, 1_700_000_000_000_000_000, str(tmp_path / "hermes.exe"), "a" * 16)
    assert state["alive"]
    assert not state["owned"], state
