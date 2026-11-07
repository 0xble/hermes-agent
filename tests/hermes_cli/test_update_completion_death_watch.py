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
from pathlib import Path

import pytest

from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("posix")


def _alive(pid: int) -> bool:
    if sys.platform.startswith("linux"):
        try:
            # An exited orphan can remain a zombie until container PID 1 reaps
            # it; it holds no checkout lock and is not a surviving completion.
            if (Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]) == "Z":
                return False
        except (OSError, IndexError):
            pass
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
        wait_until(lambda: not _alive(child_pid), timeout=5,
                   what="completion session to die with its killed updater")
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


def test_watcher_spawn_failure_does_not_launch_unfenced_completion(tmp_path, monkeypatch):
    from hermes_cli import update_completion

    launches = []

    def refuse_watcher(argv, **kwargs):
        launches.append(argv)
        raise OSError("watcher refused")

    # The optional libatomic pre-install is unrelated to completion custody.
    monkeypatch.setattr(update_completion.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(update_completion.subprocess, "Popen", refuse_watcher)
    with pytest.raises(OSError, match="watcher refused"):
        update_completion.run_completion({"source": str(tmp_path), "home": str(tmp_path / "home")})
    assert len(launches) == 1, "completion was launched after its death watch failed"
    assert launches[0][-1] == update_completion._OWNER_DEATH_WATCH
