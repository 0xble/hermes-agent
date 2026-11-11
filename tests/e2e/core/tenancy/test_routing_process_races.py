"""Ownership must be re-proved before signalling a process during teardown."""
from __future__ import annotations

from unittest.mock import Mock

import psutil
import pytest

from . import _routing_processes as cleanup


@pytest.fixture
def process(monkeypatch):
    proc = Mock(pid=12345)
    proc.create_time.return_value = 100.0
    current = Mock()
    current.create_time.return_value = 100.0
    monkeypatch.setattr(cleanup.psutil, "Process", lambda pid=None: current)
    return proc, current


def test_discovery_pins_identity_before_proving_home(monkeypatch, tmp_path, process):
    proc, current = process
    current.username.return_value = "case-user"
    proc.info = {"username": "case-user", "status": psutil.STATUS_RUNNING}

    def environ():
        proc.create_time.assert_called_once_with()
        return {"HERMES_HOME": str(tmp_path / "home")}

    proc.environ.side_effect = environ
    monkeypatch.setattr(cleanup.psutil, "process_iter", lambda *args, **kwargs: [proc])

    assert cleanup.case_processes(tmp_path) == [proc]


@pytest.mark.parametrize("phase", ["terminate", "kill"])
@pytest.mark.parametrize("after_denial", ["gone", "zombie", "running", "denied"])
def test_teardown_rechecks_denied_environment(monkeypatch, tmp_path, process, phase, after_denial):
    proc, _current = process
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
def test_teardown_preserves_ownership_and_survivor_failures(monkeypatch, tmp_path, process, home):
    proc, _current = process
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


@pytest.mark.parametrize("signal", ["terminate", "kill"])
@pytest.mark.parametrize("environment", [{}, {"OTHER": "value"}, {"HERMES_HOME": ""}])
@pytest.mark.parametrize("after_read", ["gone", "zombie", "dead", "reused", "running", "denied"])
def test_missing_home_rechecks_identity_and_liveness(tmp_path, process, signal, environment, after_read):
    proc, current = process
    proc.status.return_value = psutil.STATUS_RUNNING

    def environ():
        if after_read == "reused":
            current.create_time.return_value = 200.0
        proc.status.side_effect = {
            "gone": psutil.NoSuchProcess(proc.pid),
            "denied": psutil.AccessDenied(proc.pid),
        }.get(after_read)
        proc.status.return_value = {
            "zombie": psutil.STATUS_ZOMBIE,
            "dead": psutil.STATUS_DEAD,
        }.get(after_read, psutil.STATUS_RUNNING)
        return environment

    proc.environ.side_effect = environ

    assert cleanup._signal_case_process(proc, tmp_path, signal) is (after_read in ("running", "denied"))
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()


@pytest.mark.parametrize("signal", ["terminate", "kill"])
@pytest.mark.parametrize("owner", ["foreign", "reused", "reused_after_read", "owned"])
def test_signal_requires_current_owned_identity(tmp_path, process, signal, owner):
    proc, current = process
    proc.status.return_value = psutil.STATUS_RUNNING
    home = tmp_path.parent if owner == "foreign" else tmp_path / "home"

    def environ():
        if owner == "reused_after_read":
            current.create_time.return_value = 200.0
        return {"HERMES_HOME": str(home)}

    proc.environ.side_effect = environ
    if owner == "reused":
        current.create_time.return_value = 200.0

    if owner == "foreign":
        with pytest.raises(AssertionError, match="refusing PID"):
            cleanup._signal_case_process(proc, tmp_path, signal)
    else:
        assert cleanup._signal_case_process(proc, tmp_path, signal) is (owner == "owned")

    assert proc.terminate.call_count == (owner == "owned" and signal == "terminate")
    assert proc.kill.call_count == (owner == "owned" and signal == "kill")
