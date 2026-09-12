"""Outgoing gateway identity is pending until the drain budget or replacement.

Exercise the real fleet collector, verifier, and command-boundary receipt against
isolated profile state; only runtime identity and time are simulated.
"""
from types import SimpleNamespace
import json

import pytest

from hermes_cli import gateway, main, update_cmd, update_cmd_fleet as fleet, update_receipt as receipt


@pytest.mark.macos_only
@pytest.mark.parametrize("cron_timeout, replacement_at", [(30, 76.0), (45, 91.0), (0, 51.0)])
@pytest.mark.parametrize("replacement", ["current", "stale", "never", "down", "empty"])
def test_restart_verification_waits_for_long_drain_and_finalizes(
    monkeypatch, tmp_path, replacement, cron_timeout, replacement_at,
):
    from hermes_constants import get_hermes_home

    # Real profile config -> CLI resolver -> fleet deadline. The chronology is
    # after-turn (30s), chat/cron drain, then interruption/cleanup/startup (16s).
    # In particular, the default cron successor appears after the old 50s cap.
    (get_hermes_home() / "config.yaml").write_text(
        f"agent:\n  restart_drain_timeout: 5\n  restart_after_turn_timeout: 30\n"
        f"  cron_drain_timeout: {cron_timeout}\n", encoding="utf-8",
    )
    for key in ("HERMES_RESTART_DRAIN_TIMEOUT", "HERMES_RESTART_AFTER_TURN_TIMEOUT", "HERMES_CRON_DRAIN_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    budget = gateway._get_restart_exit_wait_budget()
    expected_sha, old_sha = "a" * 40, "b" * 40
    clock = SimpleNamespace(now=0.0)
    probe_times = []

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(fleet, "_time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(receipt, "_code_identity", lambda **kwargs: {"sha": expected_sha})
    monkeypatch.setattr(receipt, "_profile_homes", lambda: [("default", tmp_path)])

    def identity(home):
        probe_times.append(clock.now)
        if clock.now < replacement_at or replacement == "never":
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
    # Exercise the real snapshot producer, not an already-correct PID list.
    # A gateway-descendant updater's cleanup scan skips its ancestor; launchd
    # contributes the stderr wrapper instead of the socket-owning gateway.
    from hermes_cli import update_inventory
    parents = {gateway.os.getpid(): 17178, 17178: 17177, 17177: 1}
    monkeypatch.setattr(gateway, "_get_parent_pid", parents.get)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr(gateway, "launchd_gateway_labels_for_install", lambda: ["ai.hermes.gateway"])
    monkeypatch.setattr(gateway.os.path, "isdir", lambda path: False)

    def process_listing(argv, **kwargs):
        if argv[:2] == ["launchctl", "print"]:
            return SimpleNamespace(returncode=0, stdout="state = running\n pid = 17177\n")
        if argv == ["launchctl", "list"]:
            return SimpleNamespace(returncode=0, stdout="17177 0 ai.hermes.gateway\n")
        assert argv == ["ps", "-Aww", "-o", "pid=,command="]
        return SimpleNamespace(returncode=0, stdout=(
            "17177 python stderr_wrapper.py\n"
            "17178 python -m hermes_cli.main gateway run --external-supervisor\n"
        ))

    monkeypatch.setattr(gateway.subprocess, "run", process_listing)
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda: [])
    assert gateway.find_gateway_pids(all_profiles=True) == [17177]
    plan = update_inventory.UpdatePlan(profiles=["default"])
    update_inventory._collect_gateway_runtimes(plan, [("default", tmp_path)], set())
    assert [(r.profile, r.pid) for r in plan.runtimes] == [("default", 17178)]
    monkeypatch.setattr(main, "_purge_stale_hermes_modules", lambda: None)
    monkeypatch.setattr(update_cmd, "_restart_macos_launchd_gateways", lambda: ["ai.hermes.gateway"])
    monkeypatch.setattr(fleet, "_restart_manual_gateways", lambda *args: None)
    monkeypatch.setattr(fleet, "_force_kill_stuck_gateways", lambda *args: None)
    monkeypatch.setattr(update_cmd, "_write_gateway_update_exit_code", lambda code: None)
    restart = fleet._restart_gateway_fleet_after_update(plan, gateway_mode=False)
    assert set(restart.pre_restart_gateway_pids) == {17177, 17178}

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
        assert replacement_at <= clock.now <= replacement_at + 2
        assert clock.now < budget
        assert saved["fleet"][0]["pid"] == 91110
        assert saved["fleet"][0]["state"] == replacement
    else:
        assert budget <= clock.now <= budget + 2
    assert len(probe_times) > 1
    # Feed the finalized real command receipt to the native notice interpreter.
    # No process marker means "pending", not a premature failure or success.
    from gateway.update_notifications import final_outcome, notice
    home = get_hermes_home()
    pending = {"notification_version": 2, "timestamp": saved["started_at"], "reason": "Apply fix"}
    assert final_outcome(home, pending) is None
    (home / ".update_process_exit_code").write_text("0" if replacement == "current" else "1")
    success, detail = final_outcome(home, pending)
    assert success == (replacement == "current")
    text = notice("✅ Update Complete" if success else "❌ Update Failed", pending, detail)
    assert ("Update Failed" in text) == (replacement != "current")


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
