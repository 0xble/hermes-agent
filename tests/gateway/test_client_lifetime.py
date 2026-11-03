"""A lost lifetime pipe bounds a wedged gateway, not just cooperative idle shutdown."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.waiting import wait_until


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # only the exact foreground CLI under a disposable HOME is exercised
@pytest.mark.parametrize("stdin_kind", ["null", "tty"])
def test_client_owned_cli_requires_a_lifetime_pipe(tmp_path, stdin_kind):
    import pty
    master = slave = None
    try:
        if stdin_kind == "tty":
            master, slave = pty.openpty()
        proc = subprocess.run([sys.executable, "-m", "hermes_cli.main", "gateway", "run", "--client-owned"],
                              env={**os.environ, "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path), "PYTHONPATH": str(Path.cwd())},
                              stdin=slave if slave is not None else subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", timeout=15)
        assert proc.returncode != 0
        assert "--client-owned requires stdin to be a pipe" in proc.stderr, (proc.stdout, proc.stderr)
        assert not (tmp_path / "gateway.pid").exists()
    finally:
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # exact test-owned tool grandchild can become orphaned on regression
@pytest.mark.parametrize("with_tool", [False, True])
def test_owner_eof_forces_exit_when_shutdown_signal_is_ignored(tmp_path, with_tool):
    ready = tmp_path / "ready"
    script = '''
import os, signal, subprocess, threading, sys
from pathlib import Path
from gateway.client_lifetime import arm_client_lifetime
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
arm_client_lifetime()
if sys.argv[2] == 'True':
    from tools.environments import base
    from tools.environments.local import LocalEnvironment
    assert base._enter_foreground_spawn()
    tool = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    base._leave_foreground_spawn(LocalEnvironment.__new__(LocalEnvironment), tool)
    Path(sys.argv[1]).with_suffix('.tool').write_text(str(tool.pid))
Path(sys.argv[1]).touch()
threading.Event().wait(30)
'''
    env = {**os.environ, 'HERMES_HOME': str(tmp_path), 'PYTHONPATH': str(Path.cwd())}
    child = subprocess.Popen([sys.executable, '-c', script, str(ready), str(with_tool)], env=env,
                             stdin=subprocess.PIPE)
    try:
        assert wait_until(ready.exists, timeout=10)
        child.stdin.close()
        # 74 distinguishes forced lifetime teardown from a graceful client exit.
        assert child.wait(timeout=6) == 74
        if with_tool:
            import psutil
            tool_pid = int(ready.with_suffix('.tool').read_text())
            assert wait_until(lambda: not psutil.pid_exists(tool_pid) or
                              psutil.Process(tool_pid).status() == psutil.STATUS_ZOMBIE, timeout=3)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        if ready.with_suffix('.tool').exists():
            import psutil
            tool_pid = int(ready.with_suffix('.tool').read_text())
            if psutil.pid_exists(tool_pid):
                psutil.Process(tool_pid).kill()  # only the exact tool spawned by this test
