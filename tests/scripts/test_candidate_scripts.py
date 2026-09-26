"""Focused checks for the candidate maintenance scripts (slice 14/6 follow-ups)."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _receipt(home: Path, **fields):
    directory = home / "logs" / "update_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "outcome": "success", "pre_update": {"sha": "a" * 40}, "post_update": {"sha": "b" * 40},
               "steps": [], "fleet": []}
    payload.update(fields)
    (directory / "latest.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("linked", [False, True])
def test_maintenance_installer_preserves_flat_profiles_and_nests_linked_scripts(tmp_path, linked):
    """A linked script root must not receive flat generated files in the source checkout."""
    mod = _load("install_candidate_extensions")
    home = tmp_path / "profile"
    home.mkdir()
    scripts = home / "scripts"
    if linked:
        source = tmp_path / "checkout" / "src"
        source.mkdir(parents=True)
        scripts.symlink_to(source, target_is_directory=True)
        # Existing flat file is not replaced in a future scripts checkout.
        (source / "sync_fork_candidate.py").write_text("untouched", encoding="utf-8")
    else:
        scripts.mkdir()
    expected = scripts / "hermes" if linked else scripts
    installed = mod.install_maintenance(home)
    assert installed == ["sync_fork_candidate.py", "check_fork_patches.py", "sync_fork_candidate_job.sh"]
    assert all((expected / name).is_file() for name in installed)
    assert all((expected / name).stat().st_mode & stat.S_IXUSR for name in installed)
    if linked:
        assert (scripts / "sync_fork_candidate.py").read_text(encoding="utf-8") == "untouched"
        assert not (scripts / "check_fork_patches.py").exists()
        assert not (scripts / "sync_fork_candidate_job.sh").exists()
    shell = (expected / "sync_fork_candidate_job.sh").read_text(encoding="utf-8")
    assert f'$profile_home/scripts/{"hermes/" if linked else ""}sync_fork_candidate.py' in shell

    # No HERMES_HOME: the generated forwarder still locates the installing profile
    # when moved from the flat directory into the versioned checkout's domain.
    runtime_scripts = home / "hermes-agent" / "scripts"
    runtime_scripts.mkdir(parents=True)
    (runtime_scripts / "sync_fork_candidate.py").write_text("print('forwarded')\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("HERMES_HOME", None)
    completed = subprocess.run([sys.executable, str(expected / "sync_fork_candidate.py")],
                               env=env, capture_output=True, text=True, check=True)
    assert completed.stdout.strip() == "forwarded"


def test_check_receipt_reads_the_native_structure(tmp_path, monkeypatch):
    mod = _load("check_fork_patches")
    head = "b" * 40
    monkeypatch.setattr(mod, "_git", lambda *args: head)
    monkeypatch.setattr(mod, "_live_fleet", lambda: {})  # nothing running
    _receipt(tmp_path)
    assert mod.check_receipt(tmp_path) == []
    _receipt(tmp_path, outcome="partial", steps=[{"name": "reinstall", "ok": False}])
    problems = mod.check_receipt(tmp_path)
    assert any("outcome is 'partial'" in p and "reinstall" in p for p in problems)
    _receipt(tmp_path, post_update={"sha": "c" * 40})
    assert any("post_update cccccccccccc" in p for p in mod.check_receipt(tmp_path))
    _receipt(tmp_path, fleet=[{"profile": "default", "pid": 7, "code_sha": "a" * 40, "state": "stale"}])
    assert any("state stale" in p for p in mod.check_receipt(tmp_path))
    _receipt(tmp_path, sha=head)  # the legacy shape the old check accepted
    (tmp_path / "logs/update_receipts/latest.json").write_text(json.dumps({"sha": head}), encoding="utf-8")
    assert any("not a native" in p for p in mod.check_receipt(tmp_path))


def test_check_receipt_trusts_the_live_fleet_over_a_stale_snapshot(tmp_path, monkeypatch, capsys):
    """A gateway that drained past the updater's settle window is recorded stale and the outcome
    partial, but launchd relaunched it on the new code: the live fleet, not the snapshot, decides."""
    mod = _load("check_fork_patches")
    head = "b" * 40
    monkeypatch.setattr(mod, "_git", lambda *args: head)
    stale_row = {"profile": "default", "pid": 7, "code_sha": "a" * 40, "state": "stale"}
    _receipt(tmp_path, outcome="partial", fleet=[stale_row])

    monkeypatch.setattr(mod, "_live_fleet", lambda: {"default": {"pid": 9, "code_sha": head, "state": "current"}})
    assert mod.check_receipt(tmp_path) == []
    out = capsys.readouterr().out
    assert "live gateway pid 9 verified current" in out and "outcome is 'partial'" in out

    # Live gateway on a different SHA, or none for that profile, or probe unavailable: still stale.
    for live in ({"default": {"pid": 9, "code_sha": "c" * 40, "state": "current"}}, {"other": {"pid": 9, "code_sha": head}}, {}, None):
        monkeypatch.setattr(mod, "_live_fleet", lambda live=live: live)
        problems = mod.check_receipt(tmp_path)
        assert any("state stale" in p for p in problems), live
        assert any("outcome is 'partial'" in p for p in problems), live

    # A failed step is a real failure regardless of the live fleet.
    _receipt(tmp_path, outcome="partial", steps=[{"name": "reinstall", "ok": False}], fleet=[stale_row])
    monkeypatch.setattr(mod, "_live_fleet", lambda: {"default": {"pid": 9, "code_sha": head, "state": "current"}})
    problems = mod.check_receipt(tmp_path)
    assert len(problems) == 1 and "reinstall" in problems[0]


def test_check_receipt_only_excuses_partial_when_a_row_was_verified_live(tmp_path, monkeypatch, capsys):
    """The outcome downgrade is limited to the settle-window case: a ``partial`` whose only evidence
    is a stale/down row the live fleet disproves. Nothing else non-success passes, even with no step
    records and a healthy live fleet."""
    mod = _load("check_fork_patches")
    head = "b" * 40
    monkeypatch.setattr(mod, "_git", lambda *args: head)
    healthy = {"default": {"pid": 9, "code_sha": head, "state": "current"}}
    monkeypatch.setattr(mod, "_live_fleet", lambda: healthy)

    for outcome in ("failed", "refused", "running", "partial", ""):
        _receipt(tmp_path, outcome=outcome)  # no steps, no fleet rows: nothing was verified live
        problems = mod.check_receipt(tmp_path)
        assert len(problems) == 1 and f"outcome is {outcome or 'missing'!r}" in problems[0], outcome
    assert "verified current" not in capsys.readouterr().out

    # A down row (updater killed it, nothing replaced it) is treated like stale: live fleet decides.
    down_row = {"profile": "default", "pid": 7, "code_sha": None, "state": "down"}
    _receipt(tmp_path, outcome="partial", fleet=[down_row])
    assert mod.check_receipt(tmp_path) == []
    monkeypatch.setattr(mod, "_live_fleet", lambda: {})
    problems = mod.check_receipt(tmp_path)
    assert any("state down" in p for p in problems) and any("outcome is 'partial'" in p for p in problems)

    # Probe unavailable is said so, not silently treated as verified.
    monkeypatch.setattr(mod, "_live_fleet", lambda: None)
    problems = mod.check_receipt(tmp_path)
    assert any("live fleet probe unavailable" in p for p in problems)

    # Two stale rows, only one verified live: the unverified profile still fails, and so does the outcome.
    monkeypatch.setattr(mod, "_live_fleet", lambda: healthy)
    _receipt(tmp_path, outcome="partial", fleet=[
        {"profile": "default", "pid": 7, "code_sha": "a" * 40, "state": "stale"},
        {"profile": "lpg", "pid": 8, "code_sha": "a" * 40, "state": "stale"},
    ])
    problems = mod.check_receipt(tmp_path)
    assert any("'lpg'" in p and "state stale" in p for p in problems)
    assert any("outcome is 'partial'" in p for p in problems)
    assert not any("'default'" in p for p in problems)


def test_check_receipt_success_ignores_opted_out_steps(tmp_path, monkeypatch):
    """The updater records an opted-out backup as ``ok: false`` and still finalizes success."""
    mod = _load("check_fork_patches")
    head = "b" * 40
    monkeypatch.setattr(mod, "_git", lambda *args: head)
    monkeypatch.setattr(mod, "_live_fleet", lambda: (_ for _ in ()).throw(AssertionError("probe must not run without stale rows")))
    _receipt(tmp_path, outcome="success", steps=[{"name": "pre_update_backup", "ok": False, "detail": "disabled or failed"}],
             fleet=[{"profile": "default", "pid": 9, "code_sha": head, "state": "current"}])
    assert mod.check_receipt(tmp_path) == []


def test_check_receipt_partial_with_other_causes_is_not_excused(tmp_path, monkeypatch):
    """A live-verified stale row cannot mask the receipt's other partial causes, which are not steps."""
    mod = _load("check_fork_patches")
    head = "b" * 40
    monkeypatch.setattr(mod, "_git", lambda *args: head)
    monkeypatch.setattr(mod, "_live_fleet", lambda: {"default": {"pid": 9, "code_sha": head, "state": "current"}})
    stale_row = {"profile": "default", "pid": 7, "code_sha": "a" * 40, "state": "stale"}
    cases = {
        "failed restart units: ai.hermes.gateway-lpg": {"gateway_restart": {"failed_units": ["ai.hermes.gateway-lpg"]}},
        "restart phase incomplete (boom)": {"gateway_restart": {"incomplete": True, "phase_error": "boom"}},
        "unaccounted runtimes: lpg": {"runtime_outcomes": [{"kind": "gateway", "profile": "lpg", "outcome": "unaccounted"}]},
    }
    for expected, extra in cases.items():
        _receipt(tmp_path, outcome="partial", fleet=[stale_row], **extra)
        problems = mod.check_receipt(tmp_path)
        assert len(problems) == 1 and "outcome is 'partial'" in problems[0] and expected in problems[0], expected
    # The clean bookkeeping the settle-window case actually produces still passes.
    _receipt(tmp_path, outcome="partial", fleet=[stale_row],
             gateway_restart={"failed_units": [], "incomplete": False, "phase_error": ""},
             runtime_outcomes=[{"kind": "gateway", "profile": "default", "outcome": "restarted"}])
    assert mod.check_receipt(tmp_path) == []


def test_rollback_reinstall_failure_reaches_recovery(tmp_path):
    """The exact shell shape the rollback script uses: a failing pipeline under set -euo pipefail."""
    script = tmp_path / "shape.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nreinstall() { echo boom >&2; return 7; }\n"
        "if reinstall 2>&1 | tail -3; then :; else echo RECOVERED; exit 1; fi\necho UNREACHABLE\n")
    run = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert run.returncode == 1 and "RECOVERED" in run.stdout and "UNREACHABLE" not in run.stdout
    real = (SCRIPTS / "rollback_fork_runtime.sh").read_text()
    assert "if reinstall 2>&1 | tail -3; then" in real and "PIPESTATUS" not in real


def test_fork_patch_check_verifies_the_checkout_it_runs_from(tmp_path):
    """A candidate worktree shares the primary checkout's venv, whose editable hermes_cli resolves
    to the primary checkout; the check must still verify the worktree it was launched from."""
    import importlib.util

    checkout = tmp_path / "candidate"
    (checkout / "hermes_cli").mkdir(parents=True)
    (checkout / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (checkout / "scripts").mkdir()
    script = checkout / "scripts" / "check_fork_patches.py"
    script.write_text((SCRIPTS / "check_fork_patches.py").read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("candidate_fork_patch_check", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.REPO == checkout.resolve()

    # Copied out of a checkout (the cron --script root), it still falls back to the installed package.
    loose = tmp_path / "home" / "scripts"
    loose.mkdir(parents=True)
    copied = loose / "check_fork_patches.py"
    copied.write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("loose_fork_patch_check", copied)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import hermes_cli
    assert module.REPO == Path(hermes_cli.__file__).resolve().parents[1]


def test_check_receipt_ignores_gateways_on_a_separate_checkout(tmp_path, monkeypatch):
    """A fleet row in the updater's ``external`` state serves a checkout this update did not touch."""
    mod = _load("check_fork_patches")
    head = "b" * 40
    monkeypatch.setattr(mod, "_git", lambda *args: head)
    monkeypatch.setattr(mod, "_live_fleet", lambda: {})
    _receipt(tmp_path, fleet=[
        {"profile": "default", "pid": 9, "code_sha": head, "state": "current"},
        {"profile": "lpg", "pid": 11, "code_sha": "e" * 40, "state": "external", "code_root": "/srv/other"},
    ])
    assert mod.check_receipt(tmp_path) == []
    # An ordinary row on other code still fails; only the external state is exempt.
    _receipt(tmp_path, fleet=[{"profile": "lpg", "pid": 11, "code_sha": "e" * 40, "state": "current"}])
    assert any("'lpg'" in p for p in mod.check_receipt(tmp_path))


def test_check_config_does_not_pin_background_review(tmp_path, monkeypatch):
    """Background review is an owner choice; enabling it must not fail the fork-patch check."""
    mod = _load("check_fork_patches")
    assert "auxiliary.background_review.enabled" not in mod.EXPECTED_CONFIG
    values = {"memory.write_approval": "false", "delegation.model": "m", "auxiliary.review.model": "m",
              "auxiliary.background_review.enabled": "true"}

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=values[cmd[-1]] + "\n", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.check_config(tmp_path) == []
    values["memory.write_approval"] = "true"
    assert mod.check_config(tmp_path) == ["config memory.write_approval = 'true', expected 'false'"]
