"""Embedded cua-driver lifetime: owner exit, owner crash, and the stale-socket sweep.

Fake drivers only. These tests never signal a live cua-driver.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from tools.computer_use import cua_driver_lifetime as life


def _proc(pid, ppid, exe, socket_path, username="tester", create_time=None, extra=()):
    return life.EmbeddedProcess(
        pid=pid,
        ppid=ppid,
        username=username,
        exe=exe,
        cmdline=(exe, "serve", "--embedded", "--socket", socket_path, *extra),
        create_time=time.time() if create_time is None else create_time,
    )


@pytest.fixture
def short_tmp():
    # macOS AF_UNIX paths must stay under 104 bytes. pytest's tmp_path does not.
    path = Path(tempfile.mkdtemp(prefix="hct", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _unix_socket(path: Path) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
    finally:
        sock.close()


def _compile_fake_driver(tmp_path: Path) -> Path:
    source = tmp_path / "fake-driver.c"
    binary = tmp_path / "fake-cua-driver"
    source.write_text(textwrap.dedent("""\
        #include <signal.h>
        #include <unistd.h>
        static void stop(int sig) { (void)sig; _exit(0); }
        int main(void) {
            signal(SIGTERM, stop);
            signal(SIGINT, stop);
            for (;;) pause();
            return 0;
        }
    """), encoding="utf-8")
    try:
        subprocess.run(
            ["cc", "-o", str(binary), str(source)],
            check=True, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"cannot compile a fake driver: {exc}")
    binary.chmod(0o755)
    return binary


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _alive(pid: int) -> bool:
    try:
        import psutil
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


class TestSweepDecisions:
    def test_reaps_orphan_and_skips_live_owner(self, short_tmp):
        exe = "/tmp/hct-fake-cua-driver"
        live = short_tmp / "hc-live.sock"
        dead = short_tmp / "hc-dead.sock"
        stale = short_tmp / "hc-stale.sock"
        kept = short_tmp / "hc-kept.sock"
        plain = short_tmp / "hc-not-a-socket"
        _unix_socket(stale)
        _unix_socket(kept)
        plain.write_text("leave me", encoding="utf-8")
        life.write_owner_record(str(live), exe, pid=os.getpid())
        life.write_owner_record(str(kept), exe, pid=os.getpid())
        killed = []
        processes = [
            _proc(11, 1, exe, str(live)),
            _proc(22, 1, exe, str(dead), create_time=1.0),
            _proc(33, 50, exe, str(short_tmp / "hc-child.sock")),
            _proc(44, 1, "/tmp/not-the-driver", str(short_tmp / "hc-wrong.sock"), create_time=1.0),
            _proc(55, 1, exe, str(short_tmp / "hc-young.sock")),
        ]
        reaped = life.sweep_orphaned_embedded_drivers(
            executables={exe},
            processes=processes,
            temp_dirs=[str(short_tmp)],
            username="tester",
            kill=killed.append,
            grace_seconds=5.0,
            now=time.time(),
        )
        assert reaped == [22]
        assert killed == [22]
        assert not stale.exists()
        assert kept.exists()
        assert plain.read_text(encoding="utf-8-sig") == "leave me"
        assert life.owner_is_alive(str(live))

    def test_live_parent_client_saves_a_ppid1_driver(self, short_tmp):
        exe = "/tmp/hct-fake-cua-driver"
        socket_path = str(short_tmp / "hc-open.sock")
        client = life.EmbeddedProcess(
            pid=7, ppid=80, username="tester", exe="/usr/bin/python",
            cmdline=("/usr/bin/python", "mcp", "--socket", socket_path), create_time=1.0,
        )
        killed = []
        life.sweep_orphaned_embedded_drivers(
            executables={exe},
            processes=[_proc(9, 1, exe, socket_path, create_time=1.0), client],
            temp_dirs=[],
            username="tester",
            kill=killed.append,
            grace_seconds=0,
        )
        assert killed == []

    def test_reparented_client_does_not_save_an_orphan(self, short_tmp):
        exe = "/tmp/hct-fake-cua-driver"
        socket_path = str(short_tmp / "hc-orphan.sock")
        proxy = life.EmbeddedProcess(
            pid=8, ppid=1, username="tester", exe="/usr/bin/python",
            cmdline=("/usr/bin/python", "mcp", "--socket", socket_path), create_time=1.0,
        )
        killed = []
        life.sweep_orphaned_embedded_drivers(
            executables={exe},
            processes=[_proc(9, 1, exe, socket_path, create_time=1.0), proxy],
            temp_dirs=[],
            username="tester",
            kill=killed.append,
            grace_seconds=0,
        )
        assert killed == [9]


@pytest.mark.live_system_guard_bypass  # these tests kill session-leader and ppid-1 fakes they spawned
class TestProcessLifetime:
    def test_explicit_terminate_kills_the_driver_group(self, tmp_path, short_tmp):
        binary = _compile_fake_driver(tmp_path)
        sock = short_tmp / "hc-stop.sock"
        _unix_socket(sock)
        driver = subprocess.Popen(
            [str(binary), "serve", "--embedded", "--socket", str(sock)],
            start_new_session=True,
        )
        try:
            assert _wait_until(lambda: _alive(driver.pid))
            assert life.terminate_embedded_driver(str(sock), str(binary))
            assert _wait_until(lambda: not _alive(driver.pid))
            assert not sock.exists()
            assert _alive(os.getpid())
        finally:
            if _alive(driver.pid):
                life.kill_process_group(driver.pid)
            driver.wait(timeout=2)

    def test_watchdog_reaps_driver_when_owner_is_killed(self, tmp_path, short_tmp):
        binary = _compile_fake_driver(tmp_path)
        sock = short_tmp / "hc-crash.sock"
        _unix_socket(sock)
        driver = subprocess.Popen(
            [str(binary), "serve", "--embedded", "--socket", str(sock)],
            start_new_session=True,
        )
        owner = subprocess.Popen(
            [sys.executable, "-m", "tools.computer_use.cua_driver_lifetime", "--hold", str(sock), str(binary)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert owner.stdout is not None
            assert owner.stdout.readline().strip() == "ready"
            os.kill(owner.pid, 9)  # SIGKILL: atexit must not be required
            assert _wait_until(lambda: not _alive(driver.pid) and not sock.exists(), timeout=8.0)
        finally:
            if _alive(driver.pid):
                life.kill_process_group(driver.pid)
            if _alive(owner.pid):
                os.kill(owner.pid, 9)
            driver.wait(timeout=2)
            owner.wait(timeout=2)

    def test_closing_the_pipe_while_owner_lives_does_not_kill_the_driver(self, tmp_path, short_tmp):
        binary = _compile_fake_driver(tmp_path)
        sock = short_tmp / "hc-grace.sock"
        _unix_socket(sock)
        driver = subprocess.Popen(
            [str(binary), "serve", "--embedded", "--socket", str(sock)],
            start_new_session=True,
        )
        watch = life.arm_owner(str(sock), str(binary))
        try:
            watch.close()
            time.sleep(0.4)
            assert _alive(driver.pid)
            assert sock.exists()
        finally:
            life.terminate_embedded_driver(str(sock), str(binary))
            driver.wait(timeout=2)

    def test_startup_sweep_reaps_a_real_ppid1_orphan_and_keeps_a_live_owner(self, tmp_path, short_tmp):
        binary = _compile_fake_driver(tmp_path)
        orphan = short_tmp / "hc-ppid1.sock"
        owned = short_tmp / "hc-owned.sock"
        _unix_socket(orphan)
        _unix_socket(owned)
        orphan_pid = _spawn_orphaned(binary, orphan)
        owned_pid = _spawn_orphaned(binary, owned)
        life.write_owner_record(str(owned), str(binary), pid=os.getpid())
        try:
            assert _wait_until(lambda: _ppid(orphan_pid) == 1 and _ppid(owned_pid) == 1)
            reaped = life.sweep_orphaned_embedded_drivers(
                executables={str(binary.resolve())},
                temp_dirs=[str(short_tmp)],
                grace_seconds=0,
            )
            assert orphan_pid in reaped
            assert _wait_until(lambda: not _alive(orphan_pid))
            assert _alive(owned_pid)
            assert not orphan.exists()
            assert owned.exists()
        finally:
            for pid in (orphan_pid, owned_pid):
                if _alive(pid):
                    life.kill_process_group(pid)


def _ppid(pid: int) -> int:
    try:
        import psutil
        return int(psutil.Process(pid).ppid())
    except Exception:
        return -1


def _spawn_orphaned(binary: Path, socket_path: Path) -> int:
    """Double-fork a fake driver so its ppid is 1, and return that pid."""
    script = textwrap.dedent("""\
        import os, sys
        binary, socket_path, pidfile = sys.argv[1:]
        pid = os.fork()
        if pid == 0:
            os.setsid()
            child = os.fork()
            if child == 0:
                os.execv(binary, [binary, "serve", "--embedded", "--socket", socket_path])
            with open(pidfile, "w", encoding="utf-8") as handle:
                handle.write(str(child))
            os._exit(0)
        os.waitpid(pid, 0)
    """)
    pidfile = socket_path.with_suffix(".pid")
    subprocess.run(
        [sys.executable, "-c", script, str(binary), str(socket_path), str(pidfile)],
        check=True, timeout=10,
    )
    return int(pidfile.read_text(encoding="utf-8-sig").strip())


def test_socket_parser_rejects_non_hc_and_non_serve():
    assert life.socket_from_argv(("cua-driver", "serve", "--embedded", "--socket", "/tmp/hc-a.sock")) == "/tmp/hc-a.sock"
    assert life.socket_from_argv(("cua-driver", "serve", "--embedded", "--socket", "/tmp/other.sock")) is None
    assert life.socket_from_argv(("cua-driver", "mcp", "--embedded", "--socket", "/tmp/hc-a.sock")) is None
