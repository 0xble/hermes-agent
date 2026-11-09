"""The routing harness must stop its helpers before losing their session leader."""

import os
import subprocess
import sys

import psutil
import pytest

from tests.e2e.core.tenancy._routing_helpers import TuiGateway
from tests.waiting import wait_until


@pytest.mark.platforms("posix")
def test_tui_cleanup_keeps_ownership_until_helpers_are_stopped(tmp_path):
    # Only this test owns the writer. EOF retires the helper even on a red run
    # or if the test process is killed, without signalling an unregistered orphan.
    reader, writer = os.pipe()
    helper = f"import os; os.read({reader}, 1)"
    script = (
        "import subprocess, sys\n"
        f"child = subprocess.Popen([sys.executable, '-c', {helper!r}], "
        f"stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=({reader},))\n"
        "print(child.pid, flush=True)\n"
        "sys.stdin.read()\n"
    )
    gateway = TuiGateway.__new__(TuiGateway)
    gateway._stderr = (tmp_path / "gateway.stderr").open("w")
    gateway.proc = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=gateway._stderr, text=True, start_new_session=True, pass_fds=(reader,),
    )
    os.close(reader)
    child = psutil.Process(int(gateway.proc.stdout.readline()))

    def stopped():
        return not child.is_running() or child.status() == psutil.STATUS_ZOMBIE

    try:
        gateway.close()
        assert gateway.proc.returncode is not None
        assert wait_until(stopped, timeout=10), "gateway cleanup left its helper running"
    finally:
        os.close(writer)
        # EOF retires the dummy leader and helper on red.
        if not gateway.proc.stdin.closed:
            gateway.proc.stdin.close()
        gateway.proc.wait(timeout=10)
        assert wait_until(stopped, timeout=10)
        gateway.proc.stdout.close()
        gateway._stderr.close()
