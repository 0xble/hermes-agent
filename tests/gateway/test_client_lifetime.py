"""A lost lifetime pipe bounds a wedged gateway, not just cooperative idle shutdown."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.waiting import wait_until


@pytest.mark.platforms("posix")
def test_owner_eof_forces_exit_when_shutdown_signal_is_ignored(tmp_path):
    ready = tmp_path / "ready"
    script = '''
import signal, threading, sys
from pathlib import Path
from gateway.client_lifetime import arm_client_lifetime
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
arm_client_lifetime()
Path(sys.argv[1]).touch()
threading.Event().wait(30)
'''
    env = {**os.environ, 'HERMES_HOME': str(tmp_path), 'PYTHONPATH': str(Path.cwd())}
    child = subprocess.Popen([sys.executable, '-c', script, str(ready)], env=env,
                             stdin=subprocess.PIPE)
    try:
        assert wait_until(ready.exists, timeout=10)
        child.stdin.close()
        assert child.wait(timeout=6) == 0
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
