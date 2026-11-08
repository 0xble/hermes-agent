"""The session teardown stops processes left running against this run's Hermes homes.

A test that runs a real ``hermes chat -q`` child gets a grandchild ``gateway run`` in its own
session; no per-test subprocess patch sees it, and pytest's tmp cleanup removes only directories.
"""
from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests import conftest as suite_conftest
from tests.waiting import wait_until


def _detached(env_home, marker):
    code = "import time\nwhile True: time.sleep(0.2)\n"
    env = {**os.environ, "HERMES_HOME": str(env_home), "LIVE_REAP_MARKER": marker}
    return subprocess.Popen([sys.executable, "-c", code], env=env, start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.mark.platforms("linux", "macos")
@pytest.mark.live_system_guard_bypass  # signals only the two children this test spawned
def test_session_teardown_terminates_daemons_under_the_session_basetemp_only(tmp_path, monkeypatch):
    import psutil

    basetemp, elsewhere = tmp_path / "basetemp", tmp_path / "elsewhere"
    (basetemp / "t0" / "hermes").mkdir(parents=True)
    elsewhere.mkdir()
    inside = _detached(basetemp / "t0" / "hermes", "inside")
    outside = _detached(elsewhere, "outside")
    # Exercise real child environments without probing unrelated host processes.
    monkeypatch.setattr(psutil, "process_iter", lambda: iter([
        psutil.Process(inside.pid), psutil.Process(outside.pid),
    ]))
    try:
        # Both children have exec'd with their HERMES_HOME (what the reaper reads via psutil environ()).
        wait_until(lambda: {inside.pid, outside.pid} <= set(suite_conftest._processes_with_hermes_home_under(
            [basetemp, elsewhere])), timeout=10, what='both detached children started')
        config = SimpleNamespace(_tmp_path_factory=SimpleNamespace(_basetemp=basetemp))
        reaped = suite_conftest._reap_session_hermes_processes(config)
        assert inside.pid in reaped and outside.pid not in reaped
        assert inside.wait(timeout=5) is not None
        assert outside.poll() is None, "a process outside this session's homes was signalled"
    finally:
        for proc in (inside, outside):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EPERM])
@pytest.mark.parametrize("use_root_alias", [False, True])
def test_session_reaper_skips_wrapped_permission_error_and_keeps_home_boundary(
    tmp_path, monkeypatch, error_number, use_root_alias,
):
    import psutil

    basetemp = tmp_path / "basetemp"
    basetemp.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(basetemp, target_is_directory=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = basetemp / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    calls = []

    def unreadable():
        raise SystemError(
            "<built-in function proc_environ> returned a result with an exception set"
        ) from PermissionError(error_number, "sysctl(KERN_PROCARGS2)")

    processes = [
        SimpleNamespace(pid=os.getpid()),
        SimpleNamespace(pid=10001, environ=unreadable),
        SimpleNamespace(pid=10002, environ=lambda: {"HERMES_HOME": str(basetemp / "hermes")}),
        SimpleNamespace(pid=10003, environ=lambda: {"HERMES_HOME": str(tmp_path / "basetemp-other")}),
        SimpleNamespace(pid=10004, environ=lambda: {"HERMES_HOME": str(outside)}),
        SimpleNamespace(pid=10005, environ=lambda: {"HERMES_HOME": str(alias / "hermes")}),
        SimpleNamespace(pid=10006, environ=lambda: {"HERMES_HOME": str(escape)}),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda: iter(processes))
    monkeypatch.setattr(suite_conftest.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    config = SimpleNamespace(_tmp_path_factory=SimpleNamespace(_basetemp=alias if use_root_alias else basetemp))

    expected = [10002, 10005]
    assert suite_conftest._reap_session_hermes_processes(config) == expected
    assert calls == [(pid, signal.SIGTERM) for pid in expected]


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("message,cause", [
    ("<built-in function proc_environ> returned a result with an exception set", None),
    ("<built-in function proc_environ> returned a result with an exception set", RuntimeError("broken")),
    ("<built-in function proc_cmdline> returned a result with an exception set", PermissionError(errno.EACCES, "denied")),
    ("unrelated native failure", PermissionError(errno.EACCES, "denied")),
])
def test_session_reaper_propagates_unrelated_native_errors(tmp_path, monkeypatch, message, cause):
    import psutil

    failure = SystemError(message)

    def unreadable():
        raise failure from cause

    monkeypatch.setattr(psutil, "process_iter", lambda: iter([
        SimpleNamespace(pid=10001, environ=unreadable),
    ]))
    with pytest.raises(SystemError) as raised:
        suite_conftest._processes_with_hermes_home_under([tmp_path])
    assert raised.value is failure
