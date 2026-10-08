"""Regression coverage for the updater's external-supervisor sweep."""

from types import SimpleNamespace

import hermes_cli.gateway as gateway
import hermes_cli.update_cmd_fleet as fleet


def _outcome(pre_restart_gateway_pids):
    return SimpleNamespace(
        killed_pids=set(),
        stopped_unmapped_pids=set(),
        pre_restart_gateway_pids=pre_restart_gateway_pids,
        restarted_services=[],
        relaunched_profiles=[],
        externally_supervised_profiles=[],
        self_restart_pending_pids=set(),
    )


def _patch_sweep(monkeypatch, candidates, profile_processes, *, external_pids=(), argv_markers=()):
    signals = []
    monkeypatch.setattr(gateway, "_get_service_pids", lambda all_profiles=False: set())
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kwargs: list(candidates))
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **kwargs: list(profile_processes))
    monkeypatch.setattr(
        "hermes_cli.gateway_supervised_restart.gateway_declares_external_supervisor",
        lambda pid, home=None: pid in external_pids,
    )
    monkeypatch.setattr(
        gateway,
        "_capture_gateway_argv",
        lambda pid: ["gateway", "run", "--external-supervisor"] if pid in argv_markers else ["gateway", "run"],
    )
    monkeypatch.setattr(gateway, "_prepare_profile_gateway_update_restart", lambda profile, pid: "external-supervisor")
    monkeypatch.setattr(fleet, "_scoped_manual_gateway_pids", lambda pids, **kwargs: list(pids))
    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update", lambda *args, **kwargs: True)
    monkeypatch.setattr(fleet, "os", SimpleNamespace(kill=lambda pid, sig: signals.append((pid, sig))))
    monkeypatch.setattr(gateway, "_wait_for_gateway_exit", lambda **kwargs: None)
    return signals


def test_fresh_external_supervisor_is_protected_when_service_probe_lags(monkeypatch, tmp_path):
    """A launchd child is not manual merely because service PID discovery is briefly empty."""
    home = tmp_path / ".hermes"
    home.mkdir()
    launchd_pid = 99851
    manual_pid = 700
    profile_process = SimpleNamespace(profile="default", path=home, pid=launchd_pid)
    signals = _patch_sweep(
        monkeypatch,
        [launchd_pid, manual_pid],
        [profile_process],
        external_pids={launchd_pid},
    )

    outcome = _outcome([])
    fleet._restart_manual_gateways(outcome, 45.0)

    assert [pid for pid, _sig in signals] == [manual_pid]
    assert outcome.killed_pids == {manual_pid}
    assert outcome.externally_supervised_profiles == []


def test_preexisting_mapped_external_supervisor_is_drained_and_handed_back(monkeypatch, tmp_path):
    """An old mapped supervised gateway still exits so its supervisor loads new code."""
    home = tmp_path / ".hermes"
    home.mkdir()
    supervised_pid = 4242
    profile_process = SimpleNamespace(profile="fitness", path=home, pid=supervised_pid)
    signals = _patch_sweep(
        monkeypatch,
        [supervised_pid],
        [profile_process],
        external_pids={supervised_pid},
    )

    outcome = _outcome([supervised_pid])
    fleet._restart_manual_gateways(outcome, 45.0)

    assert signals == []
    assert outcome.killed_pids == {supervised_pid}
    assert outcome.externally_supervised_profiles == ["fitness"]


def test_preexisting_unmapped_external_supervisor_is_signalled(monkeypatch):
    """An old unmapped supervised gateway remains on the stop path."""
    supervised_pid = 5252
    signals = _patch_sweep(
        monkeypatch,
        [supervised_pid],
        [],
        argv_markers={supervised_pid},
    )

    outcome = _outcome([supervised_pid])
    fleet._restart_manual_gateways(outcome, 45.0)

    assert [pid for pid, _sig in signals] == [supervised_pid]
    assert outcome.killed_pids == {supervised_pid}
    assert outcome.stopped_unmapped_pids == {supervised_pid}


def test_truly_manual_gateway_is_stopped(monkeypatch):
    manual_pid = 700
    signals = _patch_sweep(monkeypatch, [manual_pid], [])

    outcome = _outcome([manual_pid])
    fleet._restart_manual_gateways(outcome, 45.0)

    assert [pid for pid, _sig in signals] == [manual_pid]
    assert outcome.killed_pids == {manual_pid}


def test_external_supervisor_probe_error_is_reported_and_fresh_pid_is_protected(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / ".hermes"
    home.mkdir()
    supervised_pid = 6262
    profile_process = SimpleNamespace(profile="default", path=home, pid=supervised_pid)
    signals = _patch_sweep(monkeypatch, [supervised_pid], [profile_process])
    monkeypatch.setattr(
        "hermes_cli.gateway_supervised_restart.gateway_declares_external_supervisor",
        lambda pid, home=None: (_ for _ in ()).throw(RuntimeError("identity unavailable")),
    )

    outcome = _outcome([])
    fleet._restart_manual_gateways(outcome, 45.0)

    assert signals == []
    assert outcome.killed_pids == set()
    output = capsys.readouterr().out
    assert "Could not determine external-supervisor ownership" in output
    assert str(supervised_pid) in output
