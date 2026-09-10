"""Outgoing gateway identity is pending until the drain budget or replacement.

Exercise the real fleet collector, verifier, and command-boundary receipt against
isolated profile state; only runtime identity and time are simulated.
"""
from types import SimpleNamespace
import json

import pytest

from hermes_cli import gateway, main, update_cmd, update_cmd_fleet as fleet, update_receipt as receipt


@pytest.mark.parametrize("replacement", ["current", "stale", "never", "down", "empty"])
def test_restart_verification_waits_for_long_drain_and_finalizes(monkeypatch, tmp_path, replacement):
    expected_sha, old_sha = "a" * 40, "b" * 40
    clock = SimpleNamespace(now=0.0)
    budget = 240.0
    probe_times = []

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(fleet, "_time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(gateway, "_get_restart_exit_wait_budget", lambda: budget)
    monkeypatch.setattr(receipt, "_code_identity", lambda **kwargs: {"sha": expected_sha})
    monkeypatch.setattr(receipt, "_profile_homes", lambda: [("default", tmp_path)])

    def identity(home):
        probe_times.append(clock.now)
        if clock.now < 126 or replacement == "never":
            return 17178, {"code_sha": old_sha}
        if replacement in {"down", "empty"}:
            return None
        return 91110, {"code_sha": expected_sha if replacement == "current" else old_sha}

    monkeypatch.setattr(receipt, "_socket_identity", identity)
    import gateway.status as status
    monkeypatch.setattr(status, "read_runtime_status", lambda path: {} if replacement == "empty" else {
        "pid": 17178, "gateway_state": "running", "code_sha": old_sha,
    })
    monkeypatch.setattr(status, "runtime_status_pid_is_live", lambda record: False)
    monkeypatch.setattr(fleet, "_print_legacy_units_warning", lambda: None)
    monkeypatch.setattr(update_cmd, "_finish_dashboard_update_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_surviving_pre_update_serve_runtimes", lambda plan: [])
    monkeypatch.setattr(main, "_update_preflight_handled", lambda args: False)
    monkeypatch.setattr(main, "_install_hangup_protection", lambda **kwargs: None)
    monkeypatch.setattr(main, "_finalize_update_output", lambda state: None)
    monkeypatch.setattr(receipt, "_current", None)
    restart = fleet._GatewayRestartOutcome(
        incomplete=False, phase_errors=[], pre_restart_gateway_pids=[17178],
        restarted_services=["ai.hermes.gateway"], failed_or_stale_units=[],
        relaunched_profiles=[], externally_supervised_profiles=[], killed_pids=set(),
    )

    def run_update(args, **kwargs):
        receipt.begin_update_receipt()
        fleet._write_fleet_restart_pending_marker(expected_sha=expected_sha)
        fleet._verify_fleet_after_update(
            restart, _pre_update_plan=None, _windows_gateway_resume=None,
            node_failures=[], update_complete=True,
        )

    monkeypatch.setattr(update_cmd, "_cmd_update_impl", run_update)
    if replacement == "current":
        main.cmd_update(SimpleNamespace(gateway=False))
    else:
        with pytest.raises(SystemExit) as exc:
            main.cmd_update(SimpleNamespace(gateway=False))
        assert exc.value.code == 1
    from hermes_constants import get_hermes_home
    saved = json.loads((get_hermes_home() / "logs/update_receipts/latest.json").read_text(encoding="utf-8"))
    assert saved["finished_at"] is not None
    assert saved["outcome"] == ("success" if replacement == "current" else "partial")
    assert fleet._fleet_restart_pending_marker_path().exists() == (replacement != "current")
    if replacement in {"current", "stale"}:
        assert 126 <= clock.now < budget
        assert saved["fleet"][0]["pid"] == 91110
        assert saved["fleet"][0]["state"] == replacement
    else:
        assert budget <= clock.now <= budget + 2
    assert len(probe_times) > 1


@pytest.mark.parametrize("before, outgoing", [([17178], "17178"), (["17178"], 17178), (None, 17178)])
def test_only_known_outgoing_stale_identity_is_pending(monkeypatch, before, outgoing):
    clock = SimpleNamespace(now=0.0)
    def sleep(seconds):
        clock.now += seconds
    monkeypatch.setattr(fleet, "_time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(gateway, "_get_restart_exit_wait_budget", lambda: 240.0)
    monkeypatch.setattr(receipt, "collect_fleet_versions", lambda **kwargs: [
        {"state": "stale" if clock.now < 126 else "current", "pid": outgoing if clock.now < 126 else 91110},
    ])
    rows = fleet._collect_fleet_snapshot(SimpleNamespace(pre_restart_gateway_pids=before), True)
    assert rows[0]["state"] == ("current" if before else "stale")
    assert clock.now >= 126 if before else clock.now < 30
