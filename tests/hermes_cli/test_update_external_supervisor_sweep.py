"""Regression for update sweeps killing a freshly launchd-started gateway."""

from types import SimpleNamespace

import hermes_cli.gateway as gateway
import hermes_cli.update_cmd_fleet as fleet


def test_external_supervisor_is_protected_when_service_probe_lags(monkeypatch, tmp_path):
    """A launchd child is not manual merely because service PID discovery is briefly empty."""
    home = tmp_path / ".hermes"
    home.mkdir()
    launchd_pid = 99851
    manual_pid = 700
    profile_process = SimpleNamespace(profile="default", path=home, pid=launchd_pid)
    candidates = [launchd_pid, manual_pid]
    signals = []

    # Reproduce the update ordering: launchd has started the replacement, but
    # the service-PID probe has not observed it yet.
    monkeypatch.setattr(gateway, "_get_service_pids", lambda all_profiles=False: set())
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kwargs: list(candidates))
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **kwargs: [profile_process])
    monkeypatch.setattr(
        "hermes_cli.gateway_supervised_restart.gateway_declares_external_supervisor",
        lambda pid, home=None: pid == launchd_pid,
    )
    monkeypatch.setattr(gateway, "_capture_gateway_argv", lambda pid: ["gateway", "run"])
    monkeypatch.setattr(gateway, "_prepare_profile_gateway_update_restart", lambda profile, pid: "external-supervisor")
    monkeypatch.setattr(fleet, "_scoped_manual_gateway_pids", lambda pids, **kwargs: list(pids))
    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update", lambda *args, **kwargs: True)
    monkeypatch.setattr(fleet, "os", SimpleNamespace(kill=lambda pid, sig: signals.append((pid, sig))))
    monkeypatch.setattr(gateway, "_wait_for_gateway_exit", lambda **kwargs: None)

    outcome = SimpleNamespace(
        killed_pids=set(),
        stopped_unmapped_pids=set(),
        restarted_services=[],
        relaunched_profiles=[],
        externally_supervised_profiles=[],
        self_restart_pending_pids=set(),
    )

    fleet._restart_manual_gateways(outcome, 45.0)

    assert [pid for pid, _sig in signals] == [manual_pid]
    assert outcome.killed_pids == {manual_pid}
    assert outcome.externally_supervised_profiles == []
