"""Ownership must be re-proved before signalling a process during teardown."""
from __future__ import annotations

from unittest.mock import Mock

import psutil
import pytest

from . import _routing_processes as cleanup


@pytest.mark.parametrize("phase", ["terminate", "kill"])
@pytest.mark.parametrize("after_denial", ["gone", "zombie", "running", "denied"])
def test_teardown_rechecks_denied_environment(monkeypatch, tmp_path, phase, after_denial):
    proc = Mock(pid=12345)
    denied = False

    def environ():
        nonlocal denied
        if phase == "kill" and not proc.terminate.called:
            return {"HERMES_HOME": str(tmp_path / "home")}
        denied = True
        raise psutil.AccessDenied(proc.pid)

    def status():
        if denied:
            rechecks.append(after_denial)
            if after_denial == "gone":
                raise psutil.NoSuchProcess(proc.pid)
            if after_denial == "denied":
                raise psutil.AccessDenied(proc.pid)
            if after_denial == "zombie":
                return psutil.STATUS_ZOMBIE
        return psutil.STATUS_RUNNING

    rechecks = []
    proc.environ.side_effect = environ
    proc.status.side_effect = status
    scans = iter([[proc], []])
    monkeypatch.setattr(cleanup, "case_processes", lambda _root: next(scans))
    monkeypatch.setattr(cleanup.psutil, "wait_procs", lambda processes, **kw: ([], processes))

    cleanup.stop_case_processes(tmp_path)

    assert rechecks, "AccessDenied must trigger a fresh liveness check"
    assert proc.terminate.call_count == (phase == "kill")
    proc.kill.assert_not_called()


@pytest.mark.parametrize("home", ["foreign", "owned", "owned_after_denial"])
def test_teardown_preserves_ownership_and_survivor_failures(monkeypatch, tmp_path, home):
    proc = Mock(pid=12345)
    proc.status.return_value = psutil.STATUS_RUNNING
    location = tmp_path.parent if home == "foreign" else tmp_path / "home"
    environment = {"HERMES_HOME": str(location)}
    if home == "owned_after_denial":
        proc.environ.side_effect = [psutil.AccessDenied(proc.pid)] + [environment] * 3
    else:
        proc.environ.return_value = environment
    monkeypatch.setattr(cleanup, "case_processes", lambda _root: [proc])
    monkeypatch.setattr(cleanup.psutil, "wait_procs", lambda processes, **kw: ([], processes))
    wait_until = cleanup.wait_until
    monkeypatch.setattr(cleanup, "wait_until", lambda predicate, **kw: wait_until(
        predicate, timeout=0, what=kw["what"]))

    message = "refusing PID" if home == "foreign" else "all processes with HERMES_HOME"
    with pytest.raises(AssertionError, match=message):
        cleanup.stop_case_processes(tmp_path)

    if home == "foreign":
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()
    else:
        assert proc.terminate.called
        assert proc.kill.called
