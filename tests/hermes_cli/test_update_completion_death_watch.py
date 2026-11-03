"""A SIGKILL of ``hermes update`` must take the new-session completion child with it.

Windows puts that child in a kill-on-close job. POSIX ``start_new_session`` does
not, so a crashed updater used to leave the child holding the checkout lock.
The next launch then refused (``an update is still running``) and the gateway
the update had paused stayed draining.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("posix")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_sigkill_of_the_update_parent_kills_the_completion_session(tmp_path: Path) -> None:
    """The death watch, not the parent's except-handler, reaps the child."""
    root = Path(__file__).resolve().parents[2]
    parent_py = tmp_path / "parent.py"
    pid_file = tmp_path / "child.pid"
    parent_py.write_text(textwrap.dedent(f"""\
        import os, sys, time
        sys.path.insert(0, {str(root)!r})
        from hermes_cli.update_completion import _owner_death_watch
        import subprocess
        watch = _owner_death_watch()
        if watch is None:
            raise SystemExit("watch failed to start")
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            start_new_session=True, preexec_fn=watch[0],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        open({str(pid_file)!r}, "w").write(str(child.pid))
        os.write(1, b"ready\\n")
        time.sleep(600)
    """), encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-I", str(parent_py)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert proc.stdout.readline() == b"ready\n"
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        assert child_pid != proc.pid
        assert _alive(child_pid)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _alive(child_pid):
            time.sleep(0.05)
        assert not _alive(child_pid), "completion session outlived a SIGKILL of the updater"
    finally:
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        if pid_file.is_file():
            try:
                leftover = int(pid_file.read_text(encoding="utf-8"))
            except ValueError:
                leftover = 0
            if leftover and _alive(leftover):
                os.kill(leftover, signal.SIGKILL)
