"""A lost lifetime pipe bounds a wedged gateway, not just cooperative idle shutdown."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.waiting import wait_until


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
        assert child.wait(timeout=6) == 0
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
