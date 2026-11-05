"""Liveness probes tolerate a descendant disappearing at any psutil boundary."""
import os
from unittest.mock import Mock

import psutil
import pytest

from tests import waiting


@pytest.mark.parametrize("error", [psutil.NoSuchProcess, psutil.ZombieProcess])
@pytest.mark.parametrize("probe", ["children", "cmdline"])
def test_descendant_snapshot_tolerates_reap(error, probe):
    process = Mock(spec=psutil.Process)
    getattr(process, probe).side_effect = error(123)
    helper = waiting.process_children if probe == "children" else waiting.process_cmdline
    assert helper(process) == []


@pytest.mark.parametrize("error", [psutil.NoSuchProcess, psutil.ZombieProcess])
def test_process_alive_tolerates_reap_during_status(monkeypatch, error):
    process = psutil.Process(os.getpid())
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    status = Mock(side_effect=error(process.pid))
    monkeypatch.setattr(psutil.Process, "status", status)
    assert waiting.process_alive(process) is False
    assert waiting.process_alive(process.pid) is False
    assert status.call_count == 2


@pytest.mark.parametrize("error", [psutil.NoSuchProcess, psutil.ZombieProcess])
def test_recorded_identity_tolerates_reap(error):
    process = Mock(spec=psutil.Process)
    process.is_running.return_value = True
    process.status.return_value = psutil.STATUS_SLEEPING
    process.create_time.side_effect = error(123)
    assert waiting.process_alive(process, created=123.0) is False


@pytest.mark.parametrize("status, running, expected", [
    (psutil.STATUS_SLEEPING, True, True),
    (psutil.STATUS_ZOMBIE, True, False),
    (psutil.STATUS_SLEEPING, False, False),
])
def test_process_alive_classifies_liveness(status, running, expected):
    process = Mock(spec=psutil.Process)
    process.is_running.return_value = running
    process.status.return_value = status
    assert waiting.process_alive(process) is expected


@pytest.mark.parametrize("error", [psutil.NoSuchProcess, psutil.ZombieProcess])
def test_process_alive_tolerates_reap_during_lookup(monkeypatch, error):
    monkeypatch.setattr(psutil, "Process", Mock(side_effect=error(123)))
    assert waiting.process_alive(123) is False
    waiting.kill_if_alive(123)


def test_cleanup_preserves_recorded_identity_and_waits(monkeypatch):
    process = Mock(spec=psutil.Process)
    process.is_running.return_value = True
    process.status.return_value = psutil.STATUS_SLEEPING
    process.create_time.return_value = 2.0
    monkeypatch.setattr(psutil, "Process", Mock(return_value=process))
    waiting.kill_if_alive(123, created=1.0, timeout=5)
    process.kill.assert_not_called()
    process.wait.assert_not_called()
    waiting.kill_if_alive(123, created=2.0, timeout=5)
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5)


@pytest.mark.parametrize("state", ["live", "zombie", "gone", "reaped", "kill_race", "denied", "guard"])
def test_cleanup_only_signals_live_processes(monkeypatch, state):
    process = Mock(spec=psutil.Process)
    process.is_running.return_value = state != "gone"
    process.status.return_value = psutil.STATUS_ZOMBIE if state == "zombie" else psutil.STATUS_SLEEPING
    if state == "reaped":
        process.status.side_effect = psutil.NoSuchProcess(123)
    elif state == "kill_race":
        process.kill.side_effect = psutil.NoSuchProcess(123)
    elif state == "denied":
        process.status.side_effect = psutil.AccessDenied(123)
    elif state == "guard":
        process.kill.side_effect = RuntimeError("outside the test process subtree")
    monkeypatch.setattr(psutil, "Process", Mock(return_value=process))
    if state in {"denied", "guard"}:
        with pytest.raises(psutil.AccessDenied if state == "denied" else RuntimeError):
            waiting.kill_if_alive(123)
    else:
        waiting.kill_if_alive(123)
    assert process.kill.call_count == (state in {"live", "kill_race", "guard"})
