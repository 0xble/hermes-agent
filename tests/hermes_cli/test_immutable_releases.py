"""S2 immutable-release invariants."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import immutable_releases as releases


def _fake_release(path: Path, marker: str, *, lock: str = "same") -> None:
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text(f"[project]\nname='hermes-{marker}'\n")
    (path / "uv.lock").write_text(lock)
    venv = path / ".venv" / "bin"
    venv.mkdir(parents=True)
    python = venv / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(python.stat().st_mode | stat.S_IEXEC)
    (path / ".release-ready").write_text(path.name + "\n", encoding="utf-8")


def test_promote_is_atomic_and_rollback_round_trip(tmp_path):
    home = tmp_path / ".hermes"
    a, b = home / "releases" / "a", home / "releases" / "b"
    _fake_release(a, "a")
    _fake_release(b, "b")
    releases.promote(home, a)
    before = hashlib.sha256((a / "pyproject.toml").read_bytes()).hexdigest()
    result = releases.promote(home, b)
    assert result["current"] is not None
    assert Path(result["current"]).resolve() == b.resolve()
    assert hashlib.sha256((a / "pyproject.toml").read_bytes()).hexdigest() == before
    assert (home / "previous").resolve() == a.resolve()
    releases.rollback(home)
    assert (home / "current").resolve() == a.resolve()
    assert (home / "previous").resolve() == b.resolve()


def test_failed_plugin_smoke_does_not_flip_pointer(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    a, b = home / "releases" / "a", home / "releases" / "b"
    _fake_release(a, "a")
    _fake_release(b, "b")
    releases.promote(home, a)
    monkeypatch.setattr(releases, "smoke_plugins", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad plugin")))
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='source'\n")
    (source / "uv.lock").write_text("new")
    monkeypatch.setattr(releases, "_build_venv", lambda *args, **kwargs: None)
    monkeypatch.setattr(releases, "release_sha", lambda _source: "c")
    with pytest.raises(RuntimeError, match="bad plugin"):
        releases.stage_release(source, home, uv="unused")
    assert (home / "current").resolve() == a.resolve()


def test_retention_keeps_live_and_rollback_pins(tmp_path):
    home = tmp_path / ".hermes"
    for i in range(7):
        p = home / "releases" / str(i)
        _fake_release(p, str(i))
        os.utime(p, (i, i))
    releases.promote(home, home / "releases" / "6")
    releases.promote(home, home / "releases" / "5")
    pinned = home / "releases" / "0"
    removed = releases.retain(home, extra_pins=[pinned])
    assert pinned not in removed
    assert (home / "releases" / "6").exists()
    assert (home / "releases" / "5").exists()
    assert len(list((home / "releases").iterdir())) >= 5


def test_sigkill_stage_and_flip_converge_with_complete_current(tmp_path):
    """Real child process dies at both commit boundaries; a second update converges."""
    home = tmp_path / "profile"
    source = tmp_path / "source"
    source.mkdir()
    (source / "version.txt").write_text("B", encoding="utf-8")
    a = home / "releases" / "A"
    _fake_release(a, "A")
    releases.promote(home, a)
    script = """
import os, pathlib, signal, sys, time
from hermes_cli import immutable_releases as r
source, home, checkpoint, boundary = map(pathlib.Path, sys.argv[1:])
r._build_venv = lambda *args, **kwargs: None
r.smoke_plugins = lambda *args, **kwargs: None
candidate, _ = r.stage_release(source, home, sha='B')
def pause():
    checkpoint.write_text(str(os.getpid()), encoding='utf-8')
    while True: time.sleep(.1)
if boundary.name == 'stage': pause()
r.promote(home, candidate, before_flip=pause)
"""
    for boundary in ("stage", "flip"):
        checkpoint = tmp_path / f"{boundary}.ready"
        child = subprocess.Popen([sys.executable, "-c", script, str(source), str(home),
                                  str(checkpoint), boundary], cwd=Path(__file__).resolve().parents[2])
        try:
            import time
            deadline = time.monotonic() + 20
            while not checkpoint.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            assert checkpoint.exists(), f"updater exited {child.poll()} before {boundary}"
            child.kill()  # SIGKILL, no Python finally or cleanup handlers
            assert child.wait(timeout=5) < 0
            assert (home / "current").resolve() == a
            assert (home / "current" / ".release-ready").read_text().strip() == "A"
            if boundary == "flip":
                assert (home / "previous").resolve() == a
            # Simulate rerun in the surviving process. A ready candidate is reused;
            # an incomplete one is rebuilt at its final venv path.
            from unittest.mock import patch
            with patch.object(releases, "_build_venv"), patch.object(releases, "smoke_plugins"):
                candidate, _ = releases.stage_release(source, home, sha="B")
            releases.promote(home, candidate)
            assert (home / "current").resolve() == candidate
            assert (home / "current" / ".release-ready").read_text().strip() == "B"
            releases.rollback(home)
            assert (home / "current").resolve() == a
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

@pytest.mark.macos_only
def test_existing_pointer_stale_plist_failure_restores_and_retry_repairs(tmp_path, monkeypatch):
    """A split pointer/plist left by an interrupted update is repaired on retry."""
    from hermes_cli import gateway, gateway_launchd, update_cmd
    home = tmp_path / "profile"
    a, b = home / "releases" / "a", home / "releases" / "b"
    _fake_release(a, "a")
    _fake_release(b, "b")
    releases.promote(home, a)
    plist = tmp_path / "test.plist"
    plist.write_bytes(b"source")
    monkeypatch.setattr(update_cmd.sys, "platform", "darwin")
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(releases, "release_sha", lambda path: "b")
    monkeypatch.setattr(releases, "stage_release", lambda *args, **kw: (b, "existing"))
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway, "launchd_plist_is_current", lambda: plist.read_bytes() == b"candidate")
    monkeypatch.setattr(gateway_launchd, "restore_launchd_plist", lambda body: plist.write_bytes(body) or True)
    def fail():
        plist.write_bytes(b"candidate")
        return False
    monkeypatch.setattr(gateway, "refresh_launchd_plist_if_needed", fail)
    assert not update_cmd._activate_immutable_release()
    assert (home / "current").resolve() == a
    assert not (home / "previous").exists()
    assert plist.read_bytes() == b"source"
    monkeypatch.setattr(gateway, "refresh_launchd_plist_if_needed", lambda: plist.write_bytes(b"candidate") or True)
    assert update_cmd._activate_immutable_release()
    assert (home / "current").resolve() == b
    assert (home / "previous").resolve() == a
    assert plist.read_bytes() == b"candidate"


def test_worker_environment_is_resolved_release_not_current(tmp_path):
    home = tmp_path / ".hermes"
    release = home / "releases" / "a"
    _fake_release(release, "a")
    env = releases.detached_worker_env(home, release, {"PYTHONPATH": "old"})
    assert env["HERMES_RELEASE"] == str(release.resolve())
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(release.resolve()), "old"]


def test_prepare_venv_changed_lock_builds_fresh(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    _fake_release(old, "old", lock="old")
    _fake_release(new, "new", lock="new")
    called = []
    monkeypatch.setattr(releases, "_build_venv", lambda release, uv="uv": called.append(release))
    _target, mode = releases.prepare_venv(new, old)
    assert mode == "built"
    assert called == [new]


def test_prepare_venv_identical_lock_still_builds_in_place(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    _fake_release(old, "same", lock="same")
    _fake_release(new, "same", lock="same")
    called = []
    monkeypatch.setattr(releases, "_build_venv", lambda release, uv="uv": called.append(release))
    _target, mode = releases.prepare_venv(new, old)
    assert mode == "built"
    assert called == [new]


def test_retention_protects_real_process_cwd_and_receipt(tmp_path):
    home = tmp_path / "profile"
    release_paths = [home / "releases" / str(i) for i in range(9)]
    for i, path in enumerate(release_paths):
        _fake_release(path, str(i))
        os.utime(path, (i, i))
    releases.promote(home, release_paths[8])
    releases.promote(home, release_paths[7])
    receipts = home / "logs" / "update_receipts"
    receipts.mkdir(parents=True)
    (receipts / "recovery.json").write_text(json.dumps({"release_path": str(release_paths[1])}))
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=release_paths[0])
    try:
        import psutil
        assert Path(psutil.Process(worker.pid).cwd()).resolve() == release_paths[0].resolve()
        assert release_paths[0].resolve() in releases._live_process_pins(home)
        removed = releases.retain(home)
        assert release_paths[0].exists() and release_paths[1].exists()
        assert release_paths[8].exists() and release_paths[7].exists()
        assert len([path for path in release_paths[2:7] if path.exists()]) >= 3
        assert release_paths[0] not in removed and release_paths[1] not in removed
    finally:
        worker.terminate()
        worker.wait(timeout=5)


def test_successful_receipt_history_does_not_pin_every_old_release(tmp_path):
    home = tmp_path / "profile"
    old = home / "releases" / "old"
    old.mkdir(parents=True)
    receipts = home / "logs" / "update_receipts"
    receipts.mkdir(parents=True)
    payload = {"outcome": "success", "release_transition": {"from_path": str(old)}}
    (receipts / "historical.json").write_text(json.dumps(payload))
    assert old not in releases._receipt_pins(home)
    payload["outcome"] = "partial"
    (receipts / "unfinished.json").write_text(json.dumps(payload))
    assert old in releases._receipt_pins(home)


def test_retention_failure_is_advisory_after_verified_update(tmp_path, monkeypatch):
    from hermes_cli import immutable_releases, update_cmd, update_cmd_fleet, update_receipt
    from hermes_cli.update_cmd_fleet import _GatewayRestartOutcome
    home = tmp_path / "profile"
    release = home / "releases" / "current"
    _fake_release(release, "current")
    releases.promote(home, release)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_cmd_fleet, "_print_legacy_units_warning", lambda: None)
    monkeypatch.setattr(update_cmd, "_finish_dashboard_update_cleanup", lambda *a, **kw: None)
    monkeypatch.setattr(update_cmd_fleet, "_collect_fleet_snapshot", lambda *a, **kw: [])
    monkeypatch.setattr(update_cmd_fleet, "_clear_fleet_restart_pending_marker", lambda: None)
    monkeypatch.setattr(update_cmd_fleet, "_fleet_probe_expected_runtimes", lambda *a, **kw: False)
    monkeypatch.setattr(update_cmd._m(), "_fleet_probe_expected_runtimes", lambda *a, **kw: False)
    monkeypatch.setattr(update_cmd, "_surviving_pre_update_serve_runtimes", lambda *a: [])
    monkeypatch.setattr(immutable_releases, "retain", lambda *a: (_ for _ in ()).throw(OSError("prune blocked")))
    restart = _GatewayRestartOutcome(incomplete=False, phase_errors=[], pre_restart_gateway_pids=[],
        restarted_services=[], failed_or_stale_units=[], relaunched_profiles=[],
        externally_supervised_profiles=[], killed_pids=set())
    update_receipt.begin_update_receipt()
    update_cmd_fleet._verify_fleet_after_update(restart, _pre_update_plan=None,
        _windows_gateway_resume=None, node_failures=[], update_complete=True, rollback=True)
    receipt = json.loads((home / "logs/update_receipts/latest.json").read_text())
    assert receipt["outcome"] == "success"
    assert any(s["name"] == "release_retention" and not s["ok"] and
               "prune blocked" in s["detail"] for s in receipt["steps"])


def test_verified_update_retains_real_process_pinned_old_release(tmp_path, monkeypatch):
    from hermes_cli import update_cmd, update_cmd_fleet, update_receipt
    from hermes_cli.update_cmd_fleet import _GatewayRestartOutcome
    home = tmp_path / "profile"
    releases_for_test = [home / "releases" / str(i) for i in range(7)]
    for i, release in enumerate(releases_for_test):
        _fake_release(release, str(i))
        os.utime(release, (i, i))
    releases.promote(home, releases_for_test[-1])
    releases.promote(home, releases_for_test[-2])
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                              cwd=releases_for_test[0])
    try:
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(update_cmd_fleet, "_print_legacy_units_warning", lambda: None)
        monkeypatch.setattr(update_cmd, "_finish_dashboard_update_cleanup", lambda *a, **kw: None)
        monkeypatch.setattr(update_cmd_fleet, "_collect_fleet_snapshot", lambda *a, **kw: [])
        monkeypatch.setattr(update_cmd_fleet, "_clear_fleet_restart_pending_marker", lambda: None)
        monkeypatch.setattr(update_cmd._m(), "_fleet_probe_expected_runtimes", lambda *a, **kw: False)
        monkeypatch.setattr(update_cmd, "_surviving_pre_update_serve_runtimes", lambda *a: [])
        restart = _GatewayRestartOutcome(incomplete=False, phase_errors=[], pre_restart_gateway_pids=[],
            restarted_services=[], failed_or_stale_units=[], relaunched_profiles=[],
            externally_supervised_profiles=[], killed_pids=set())
        update_receipt.begin_update_receipt()
        update_cmd_fleet._verify_fleet_after_update(restart, _pre_update_plan=None,
            _windows_gateway_resume=None, node_failures=[], update_complete=True, rollback=False)
        receipt = json.loads((home / "logs/update_receipts/latest.json").read_text())
        assert receipt["outcome"] == "success"
        assert releases_for_test[0].exists()
        assert releases_for_test[5].exists() and releases_for_test[6].exists()
        assert len([path for path in releases_for_test[1:5] if path.exists()]) == 3
        assert any(s["name"] == "release_retention" and s["ok"] for s in receipt["steps"])
    finally:
        worker.terminate()
        worker.wait(timeout=5)


@pytest.mark.parametrize("kind", ["standalone", "backend", "platform", "exclusive", "model-provider"])
def test_real_staging_rejects_incompatible_plugin_and_keeps_pointer_and_receipt(tmp_path, monkeypatch, kind):
    """Exercise checkout -> candidate venv -> plugin probe -> updater receipt, not a mocked smoke."""
    from hermes_cli import update_cmd, update_receipt

    remote = Path(__file__).resolve().parents[2]
    source = tmp_path / "hermes-agent"
    subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout",
                    str(remote), str(source)], check=True)
    sha = subprocess.check_output(["git", "-C", str(remote), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(source), "checkout", "--quiet", "--detach", sha], check=True)
    home = tmp_path / "profile"
    home.mkdir()
    plugin = home / "plugins" / "candidate-test"
    plugin.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [candidate-test]\n", encoding="utf-8")
    (plugin / "plugin.yaml").write_text(
        f"name: candidate-test\nversion: '1.0'\nkind: {kind}\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "from hermes_cli.symbol_that_does_not_exist import broken\n", encoding="utf-8")
    a = home / "releases" / "A"
    _fake_release(a, "A")
    releases.promote(home, a)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", source)
    update_receipt.begin_update_receipt()
    try:
        assert update_cmd._activate_immutable_release() is False
        update_receipt.finalize_update_receipt("partial")
        assert (home / "current").resolve() == a.resolve()
        assert (home / "previous").exists() is False
        receipt = json.loads((home / "logs" / "update_receipts" / "latest.json").read_text(encoding="utf-8"))
        assert receipt["outcome"] == "partial"
        assert any(s["name"] == "immutable_release" and not s["ok"] and
                   "candidate plugin smoke failed" in s["detail"] for s in receipt["steps"])
    finally:
        update_receipt.finalize_update_receipt("partial")


def test_candidate_import_smoke_rejects_model_provider_import_failure(tmp_path):
    home = tmp_path / "profile"
    plugin = home / "plugins" / "broken-model"
    plugin.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [broken-model]\n", encoding="utf-8")
    (plugin / "plugin.yaml").write_text(
        "name: broken-model\nversion: '1.0'\nkind: model-provider\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "raise RuntimeError('incompatible model provider')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="incompatible model provider"):
        releases.smoke_plugins(Path(__file__).resolve().parents[2], home)


def test_candidate_import_smoke_blocks_bad_enabled_plugin_without_writing_profile(tmp_path):
    home = tmp_path / "profile"
    plugin = home / "plugins" / "candidate-test"
    plugin.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [candidate-test]\n")
    (plugin / "plugin.yaml").write_text("name: candidate-test\nversion: '1.0'\n")
    source = Path(__file__).resolve().parents[2]
    (plugin / "__init__.py").write_text(
        "from hermes_cli.symbol_that_does_not_exist import broken\n"
    )
    with pytest.raises(RuntimeError, match="candidate plugin smoke failed"):
        releases.smoke_plugins(source, home)
    assert not (home / "logs").exists()
    (plugin / "__init__.py").write_text("def register(ctx):\n    pass\n")
    releases.smoke_plugins(source, home)
    assert not (home / "logs").exists()
