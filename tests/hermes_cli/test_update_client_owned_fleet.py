"""Every updater stop selection must preserve finite client leases (PR #1148)."""

import signal
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest


@pytest.fixture
def fleet_fakes(monkeypatch, tmp_path):
    from gateway import control_socket, status
    from hermes_cli import gateway, gateway_supervised_restart, update_cmd_fleet as fleet
    from hermes_cli import update_cmd_posix_pause as pause, update_fleet_scope
    from hermes_cli import gateway_client_ownership

    monkeypatch.setattr(gateway_client_ownership, "_reported_client_pids", set())
    finite, normal = 891001, 891002
    homes = {finite: tmp_path / "finite", normal: tmp_path / "normal"}
    for home in homes.values():
        home.mkdir()
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kw: [finite, normal])
    monkeypatch.setattr(gateway, "_get_service_pids", lambda **kw: set())
    monkeypatch.setattr(gateway, "_capture_gateway_argv", lambda pid: ["hermes", "gateway", "run"])
    monkeypatch.setattr(update_fleet_scope, "gateway_pid_home", homes.get)
    monkeypatch.setattr(control_socket, "identify_gateway", lambda home: {
        "pid": finite if home == homes[finite] else normal, "client_owned": home == homes[finite]})
    monkeypatch.setattr(gateway_supervised_restart, "gateway_declares_external_supervisor", lambda *a: False)
    monkeypatch.setattr(fleet, "_scoped_manual_gateway_pids", lambda pids, **kw: list(pids))
    monkeypatch.setattr(pause, "already_restarted", lambda: {"pids": set()})
    monkeypatch.setattr(gateway, "_wait_for_gateway_exit", lambda **kw: None)
    monkeypatch.setattr(fleet._time, "sleep", lambda *a: None)
    arm, drain, kill, terminate = Mock(return_value="detached"), Mock(return_value=False), Mock(), Mock()
    monkeypatch.setattr(gateway, "_prepare_profile_gateway_update_restart", arm)
    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update", drain)
    monkeypatch.setattr(fleet, "os", SimpleNamespace(kill=kill))
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: 123)
    monkeypatch.setattr(status, "terminate_pid", terminate)
    processes = {pid: SimpleNamespace(pid=pid, profile=name, path=homes[pid])
                 for pid, name in [(finite, "finite"), (normal, "normal")]}
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **kw: list(processes.values()))
    return SimpleNamespace(finite=finite, normal=normal, processes=processes, arm=arm,
                           drain=drain, kill=kill, terminate=terminate, homes=homes)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(("ownership", "mapped"), [
    ("identify", True), ("identify", False), ("argv", True), ("argv", False),
    ("mapped-home", True),
])
def test_fleet_restart_leaves_client_lease_and_restarts_normal(fleet_fakes, monkeypatch, mapped, ownership):
    from gateway import control_socket
    from hermes_cli import gateway, update_cmd_fleet as fleet

    f = fleet_fakes
    if ownership == "mapped-home":
        from hermes_cli import update_fleet_scope

        monkeypatch.setattr(update_fleet_scope, "gateway_pid_home", lambda pid: None)
    if ownership == "argv":
        monkeypatch.setattr(control_socket, "identify_gateway", lambda home: None)
        monkeypatch.setattr(gateway, "_capture_gateway_argv", lambda pid: [
            "hermes", "gateway", "run", *(["--client-owned"] if pid == f.finite else [])])
    if not mapped:
        f.processes.pop(f.finite)
    out = fleet._GatewayRestartOutcome(False, [], None, [], [], [], [], set())
    fleet._restart_manual_gateways(out, 0)
    fleet._force_kill_stuck_gateways(out.killed_pids)

    assert f.arm.call_args_list == [call("normal", f.normal)], "finite lease armed a persistent daemon"
    assert f.drain.call_args_list == [call(f.normal, 0, "normal", self_restart_pending=set())]
    assert f.kill.call_args_list == [call(f.normal, signal.SIGTERM)]
    assert f.terminate.call_args_list == [call(f.normal, force=True, expected_start_time=123)]
    assert out.killed_pids == {f.normal}
    assert out.relaunched_profiles == ["normal"]


def test_shared_ownership_requires_matching_pid_and_logs_once(fleet_fakes, capsys):
    from hermes_cli.gateway_client_ownership import is_client_owned_gateway, leave_client_owned_gateway

    f = fleet_fakes
    assert not is_client_owned_gateway(f.normal, {"pid": f.finite, "client_owned": True}, argv=[])
    assert not is_client_owned_gateway(f.normal, {}, argv=["--not-client-owned"])
    assert leave_client_owned_gateway(f.finite)
    assert leave_client_owned_gateway(f.finite)
    assert capsys.readouterr().out.count(f"left client-owned gateway {f.finite} on its lease") == 1


def test_force_kill_rechecks_client_ownership_even_in_old_killed_set(fleet_fakes):
    from hermes_cli import update_cmd_fleet as fleet

    f = fleet_fakes
    fleet._force_kill_stuck_gateways({f.finite, f.normal})
    assert f.terminate.call_args_list == [call(f.normal, force=True, expected_start_time=123)]


def test_stale_survivor_sweep_does_not_drain_client_lease(fleet_fakes):
    from hermes_cli import update_cmd_fleet as fleet, update_cmd_stale_survivors as survivors

    f = fleet_fakes
    f.drain.return_value = True
    out = fleet._GatewayRestartOutcome(False, [], None, [], [], [], [], set())
    rows = [{"pid": pid, "state": "stale", "profile": "test"} for pid in [f.finite, f.normal]]
    assert survivors.signal_stale_fleet_survivors(rows, out, 0) == [f.normal]
    assert f.drain.call_args_list == [call(f.normal, 0, f"test (PID {f.normal})")]
    assert out.killed_pids == {f.normal}


def test_windows_socket_pause_excludes_client_before_marker_or_resume_debt(fleet_fakes, monkeypatch):
    from gateway import control_socket
    from hermes_cli import gateway, update_cmd_windows as windows

    f = fleet_fakes
    monkeypatch.setattr(gateway, "find_windows_gateway_services", lambda **kw: [])
    monkeypatch.setattr(windows, "_owned_gateway_pids", lambda pids, **kw: list(pids))
    marker, pause = Mock(return_value=True), Mock(return_value={"pausing": True})
    monkeypatch.setattr(windows, "_write_update_planned_stop_marker", marker)
    monkeypatch.setattr(control_socket, "pause_gateway_for_update", pause)
    profiles, _services, service_pids, running = windows._discover_windows_gateways()
    debts, mapped, _acks = windows._request_socket_pauses(running, profiles, service_pids)

    assert debts == {"normal": f.normal}
    assert mapped == [f.normal]
    marker.assert_called_once_with(f.homes[f.normal], f.normal)
    pause.assert_called_once_with(f.homes[f.normal])


@pytest.mark.platforms("windows")
def test_windows_only_finite_lease_does_not_cold_start_daemon(fleet_fakes, monkeypatch):
    from hermes_cli import gateway, update_cmd_windows as windows

    f = fleet_fakes
    f.processes.pop(f.normal)
    monkeypatch.setattr(gateway, "find_windows_gateway_services", lambda **kw: [])
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kw: [f.finite])
    monkeypatch.setattr(windows, "_owned_gateway_pids", lambda pids, **kw: list(pids))
    from hermes_cli import update_pause_record
    monkeypatch.setattr(update_pause_record, "adopt_orphans", lambda: (None, []))
    cold_start, marker = Mock(), Mock()
    monkeypatch.setattr(windows, "_windows_cold_start_plan", cold_start)
    monkeypatch.setattr(windows, "_write_update_planned_stop_marker", marker)
    assert windows._pause_windows_gateways_for_update() is None
    cold_start.assert_not_called()
    marker.assert_not_called()


def test_windows_leftover_lease_is_not_a_tree_kill_candidate(fleet_fakes, monkeypatch):
    from hermes_cli import update_cmd_windows as windows

    f = fleet_fakes
    monkeypatch.setattr(windows, "_psutil", lambda: None)
    argv = "python -m hermes_cli.main gateway run"
    assert windows._leftover_pausable_gateway_pids([(f.finite, "python", argv)]) is None
    assert windows._leftover_pausable_gateway_pids([(f.normal, "python", argv)]) == [f.normal]
