"""Post-install qualification uses disposable release homes, never live launchd."""
import json
import os
import plistlib
from datetime import datetime, timezone, UTC
from pathlib import Path

import pytest

import hermes_cli.gateway
from hermes_cli import admission_canary as canary, gateway_guardian as guardian


@pytest.fixture
def switched(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    label = "ai.hermes.test.admission-canary"
    plist = tmp_path / f"{label}.plist"
    old, candidate = (home / "releases" / (ch * 40) for ch in "ab")
    for release in (old, candidate):
        release.mkdir(parents=True)
        for marker in (".release-ready", ".hermes_build_sha"):
            (release / marker).write_text(release.name)
    (home / "current").symlink_to(candidate)
    (home / "previous").symlink_to(old)
    plist.write_bytes(plistlib.dumps({"Label": label, "WorkingDirectory": str(candidate),
                                     "EnvironmentVariables": {"HERMES_HOME": str(home)}}))
    clock = [1000.0]
    monkeypatch.setattr(canary.time, "time", lambda: clock[0])
    monkeypatch.setattr(canary.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(guardian, "_sleep_within", lambda seconds, deadline: clock.__setitem__(0, min(deadline, clock[0] + seconds)))
    record = {"version": 1, "operation": "promote", "candidate": str(candidate),
              "previous_intended": str(old), "plist": {"path": str(plist)},
              "reload_ack": {"at": datetime.fromtimestamp(1000, UTC).isoformat(),
                             "gateway_pid": 123}}
    (home / "release-last-txn.json").write_text(json.dumps(record))
    (home / "config.yaml").write_text("gateway:\n  guardian:\n    admission_canary:\n      window_seconds: 10\n")
    monkeypatch.setattr(guardian, "_gateway_domain", lambda *a, **kw: f"gui/{os.getuid()}")
    monkeypatch.setattr(guardian, "_launch_state", lambda *a, **kw: "loaded")
    monkeypatch.setattr(guardian, "healthy", lambda *a, **kw: True)
    monkeypatch.setattr(guardian, "_refuse_leftovers_before_launch", lambda *a, **kw: None)
    rollbacks = []

    def rollback(home_arg, plist_arg, label_arg, previous, **kwargs):
        assert (home_arg, plist_arg, label_arg, previous) == (home, plist, label, old)
        rollbacks.append(previous)
        (home / "current").unlink()
        (home / "current").symlink_to(old)
        body = plistlib.loads(plist.read_bytes())
        body["WorkingDirectory"] = str(old)
        plist.write_bytes(plistlib.dumps(body))
        # Like the real rollback, replace the last transaction with its inverse.
        (home / "release-last-txn.json").write_text(json.dumps({"version": 1, "operation": "rollback"}))
        return True

    monkeypatch.setattr(guardian, "rollback_switch", rollback)
    return home, plist, label, old, candidate, clock, rollbacks


def signal(home, candidate, events, *, pid=123, sha=None):
    (home / "gateway_state.json").write_text(json.dumps({
        "code_sha": sha or candidate.name, "pid": pid,
        "native_admission": {"pid": pid, "refusals": events},
    }))


def receipts(home):
    return [json.loads(p.read_text()) for p in (home / "logs/guardian").glob("*.json")]


def test_spike_rolls_back_healthy_gateway_once_and_fails_update(switched):
    home, plist, label, old, candidate, clock, rollbacks = switched
    clock[0] = 1001
    signal(home, candidate, [[1001, "metadata"]] * 9 + [[1001, "text"]])
    with pytest.raises(canary.AdmissionCanaryError, match="10 refusals.*dominant clause=metadata"):
        canary.observe(home, candidate)
    assert (home / "current").resolve() == old
    assert rollbacks == [old]
    failure = next(r for r in receipts(home) if r["action"] == "admission_canary")
    assert failure["outcome"] == "failed" and failure["rollback_healthy"] is True
    assert failure["refusals"] == 10 and failure["dominant_clause"] == "metadata"
    assert guardian.run_once(home, plist, label) == "healthy"
    with pytest.raises(canary.AdmissionCanaryError, match="10 refusals"):
        canary.observe(home, candidate)
    assert rollbacks == [old]


@pytest.mark.parametrize("after_flip", [False, True])
def test_guardian_recovers_interrupted_canary_rollback_without_inversion(switched, monkeypatch, after_flip):
    home, plist, label, old, candidate, _, rollbacks = switched
    signal(home, candidate, [[1001, "metadata"]] * 10)
    rollback = guardian.rollback_switch
    def interrupted(*args, **kwargs):
        if after_flip:
            rollback(*args, **kwargs)
        raise SystemExit("power loss")
    monkeypatch.setattr(guardian, "rollback_switch", interrupted)
    with pytest.raises(SystemExit, match="power loss"):
        guardian.run_once(home, plist, label)
    assert canary.read_state(home)["outcome"] == "rolling_back"
    monkeypatch.setattr(guardian, "rollback_switch", rollback)
    guardian.run_once(home, plist, label)
    assert (home / "current").resolve() == old
    assert rollbacks == [old]
    assert canary.read_state(home)["outcome"] == "failed"
    assert canary.read_state(home)["rollback_healthy"] is True


def test_quiet_window_passes_without_rollback(switched):
    home, _, _, _, candidate, clock, rollbacks = switched
    canary.observe(home, candidate)
    assert clock[0] == 1010
    assert canary.read_state(home)["outcome"] == "passed"
    assert canary.read_state(home)["refusals"] == 0
    assert not rollbacks


def test_pre_ack_refusals_and_events_after_window_do_not_count(switched):
    home, _, _, _, candidate, _, rollbacks = switched
    signal(home, candidate, [[999.999, "metadata"]] * 15 + [[1011, "metadata"]] * 15)
    canary.observe(home, candidate)
    assert canary.read_state(home)["outcome"] == "passed"
    assert canary.read_state(home)["refusals"] == 0
    assert not rollbacks


@pytest.mark.parametrize("pid,sha", [(122, None), (123, "c" * 40)])
def test_other_gateway_evidence_is_excluded(switched, pid, sha):
    home, _, _, _, candidate, _, rollbacks = switched
    signal(home, candidate, [[1001, "metadata"]] * 15, pid=pid, sha=sha)
    canary.observe(home, candidate)
    assert canary.read_state(home)["outcome"] == "passed"
    assert not rollbacks


def test_off_disables_canary_even_with_spike(switched):
    home, plist, label, _, candidate, clock, rollbacks = switched
    (home / "config.yaml").write_text("gateway:\n  guardian:\n    admission_canary:\n      enabled: false\n")
    signal(home, candidate, [[1001, "metadata"]] * 15)
    canary.observe(home, candidate)
    assert guardian.run_once(home, plist, label) == "healthy"
    assert clock[0] == 1000 and not rollbacks
    assert not canary.state_path(home).exists()


def test_below_threshold_is_healthy(switched):
    home, _, _, _, candidate, _, rollbacks = switched
    signal(home, candidate, [[1001, "metadata"]] * 9)
    canary.observe(home, candidate)
    assert canary.read_state(home)["refusals"] == 9
    assert not rollbacks


@pytest.mark.parametrize("field,value", [("enabled", "yes"), ("window_seconds", 0),
    ("window_seconds", 301), ("window_seconds", float("nan")),
    ("refusal_threshold", True), ("refusal_threshold", 1001)])
def test_invalid_settings_fail_closed(switched, monkeypatch, field, value):
    home, *_ = switched
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective",
                        lambda *a, **kw: {"gateway": {"guardian": {"admission_canary": {field: value}}}})
    with pytest.raises(ValueError, match="admission_canary"):
        canary.settings(home)


def test_default_policy_is_on_with_three_minute_window(switched):
    home, *_ = switched
    (home / "config.yaml").write_text("{}\n")
    assert canary.settings(home) == (True, 180.0, 10)


def test_failed_rollback_is_still_a_failed_update(switched, monkeypatch):
    home, _, _, _, candidate, _, _ = switched
    signal(home, candidate, [[1001, "metadata"]] * 10)
    monkeypatch.setattr(guardian, "rollback_switch", lambda *a, **kw: False)
    with pytest.raises(canary.AdmissionCanaryError, match="10 refusals"):
        canary.observe(home, candidate)
    assert canary.read_state(home)["rollback_healthy"] is False
    assert sum(r["action"] == "rollback" and r["outcome"] == "attempt" for r in receipts(home)) == 1


def test_observer_deadline_bounds_locked_guardian(switched, monkeypatch):
    home, _, _, _, candidate, clock, _ = switched
    # Keep another guardian owner busy for the whole bounded wait.
    from contextlib import contextmanager
    @contextmanager
    def busy(*args):
        yield False
    monkeypatch.setattr(guardian, "inspection_lock", busy)
    with pytest.raises(canary.AdmissionCanaryError, match="timed out"):
        canary.observe(home, candidate)
    assert clock[0] <= 1070


def test_activation_reports_failure_instead_of_success_after_canary_rollback(switched, monkeypatch, capsys):
    from hermes_cli import update_cmd, immutable_releases, gateway, update_receipt
    home, plist, _, _, candidate, _, rollbacks = switched
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_immutable_release_enabled", lambda paths: True)
    monkeypatch.setattr(update_cmd, "_require_immutable_launchd", lambda: None)
    monkeypatch.setattr("hermes_cli.forward_only_guard.refuse_if_forward_only_leftovers", lambda *a, **kw: None)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway, "generate_launchd_plist", lambda **kw: plist.read_text())
    monkeypatch.setattr(immutable_releases, "stage_release", lambda *a, **kw: (candidate, "existing"))
    monkeypatch.setattr(immutable_releases, "activate_release", lambda *a, **kw: {"current": str(candidate), "previous": str(home / "releases" / ("a" * 40))})
    signal(home, candidate, [[1001, "metadata"]] * 10)
    update_receipt.begin_update_receipt()
    try:
        assert update_cmd._activate_immutable_release(sha=candidate.name, source=candidate) is False
        assert "native-admission canary failed: 10 refusals" in capsys.readouterr().out
        assert len(rollbacks) == 1
        active = update_receipt._current.get()
        assert active is not None
        assert any(step["name"] == "immutable_release" and not step["ok"] for step in active.data["steps"])
    finally:
        update_receipt._current.set(None)
