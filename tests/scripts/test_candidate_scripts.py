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


def test_check_receipt_reads_the_native_structure(tmp_path, monkeypatch):
    mod = _load("check_fork_patches")
    head = "b" * 40
    monkeypatch.setattr(mod, "_git", lambda *args: head)
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


def _fake_gh(tmp_path: Path, exit_code: int, stdout: str = "") -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\nprintf '%s\\n' {stdout!r}\nexit {exit_code}\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def test_create_pr_reports_failure_and_retire_is_idempotent(tmp_path, monkeypatch):
    mod = _load("curate_skill_observations")
    monkeypatch.setenv("PATH", f"{_fake_gh(tmp_path, 1)}:{os.environ['PATH']}")
    assert mod._create_pr(tmp_path, "skills/curate-x", "main", ["hermes"], "body") == ""
    monkeypatch.setenv("PATH", f"{_fake_gh(tmp_path, 0, 'https://github.com/0xble/dotfiles/pull/1')}:{os.environ['PATH']}")
    assert mod._create_pr(tmp_path, "skills/curate-x", "main", ["hermes"], "body").endswith("/pull/1")
    monkeypatch.setenv("PATH", f"{_fake_gh(tmp_path, 0, 'created something but no url')}:{os.environ['PATH']}")
    assert mod._create_pr(tmp_path, "skills/curate-x", "main", ["hermes"], "body") == ""
    observations = tmp_path / "obs"
    observations.mkdir()
    (observations / "hermes.md").write_text("- note\n")
    mod._retire_observations(observations, observations / "processed" / "2026-09-19", {"hermes": ["note"], "gone": []})
    assert (observations / "processed/2026-09-19/hermes.md").is_file() and not (observations / "hermes.md").exists()


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
