"""Finite-lease gateways never enter the updater's stop/replay obligations."""

import os
import signal
from unittest.mock import Mock

import pytest


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("ownership", ["identify", "argv", "bare"])
def test_pause_discovery_preserves_client_lease_and_normal_bare_replay(tmp_path, monkeypatch, ownership):
    from gateway import control_socket
    from hermes_cli import gateway, update_cmd_fleet, update_cmd_posix_pause as pause, update_fleet_scope
    from hermes_cli import update_pause_record as record

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pid = os.getpid()
    argv = ["hermes", "gateway", "run"] + (["--client-owned"] if ownership == "argv" else [])
    monkeypatch.setattr(pause, "_discover_systemd", list)
    monkeypatch.setattr(pause, "_discover_launchd", list)
    monkeypatch.setattr(gateway, "_get_service_pids", lambda **kw: [])
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kw: [pid])
    monkeypatch.setattr(update_cmd_fleet, "_scoped_manual_gateway_pids", lambda pids: pids)
    monkeypatch.setattr(pause, "_supervisor_markers", lambda pid: None)
    monkeypatch.setattr(gateway, "_capture_gateway_argv", lambda pid: argv)
    monkeypatch.setattr(update_fleet_scope, "gateway_pid_home", lambda pid: tmp_path)
    monkeypatch.setattr(control_socket, "identify_gateway", lambda *a, **kw: (
        None if ownership == "argv" else {"pid": pid, "client_owned": ownership == "identify"}))
    drain = Mock(return_value={"pausing": True, "already_stopping": False})
    monkeypatch.setattr(control_socket, "pause_gateway_for_update", drain)
    kill = Mock()
    replay = Mock(return_value=pid + 1)
    monkeypatch.setattr(pause.os, "kill", kill)
    monkeypatch.setattr(pause, "_drain_budget", lambda: 0)
    monkeypatch.setattr(pause, "_wait_gone", lambda *a: set())
    monkeypatch.setattr(pause, "_relaunch_bare", replay)
    monkeypatch.setattr(pause, "_RESTARTED", {"units": set(), "labels": set(), "pids": set()})
    token = pause._empty_token()
    assert pause._pause(token) is None
    if ownership != "bare":
        assert token == pause._empty_token(), "a finite lease acquired restart debt"
        assert record.orphans() == []
        drain.assert_not_called()
        kill.assert_not_called()
    else:
        assert token["resume_needed"] is True
        assert token["unmapped"][0]["argv"] == argv
        drain.assert_called_once_with(tmp_path)
        kill.assert_called_once_with(pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only test
    pause.resume_paused_set(token)
    if ownership != "bare":
        replay.assert_not_called()
    else:
        replay.assert_called_once()
    assert token["resume_needed"] is False
    assert token["unmapped"] == []
    assert token["unmapped_pids"] == []
    record.discharge(token)
