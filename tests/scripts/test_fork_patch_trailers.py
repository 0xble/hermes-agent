"""Exercise fork-patch commit classification: trailers above the floor, unit ownership of identities."""

import importlib.util
from pathlib import Path
import subprocess


def _checker(repo, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_fork_patches.py"
    spec = importlib.util.spec_from_file_location("fork_patch_checker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "REPO", repo)
    return module


def _repo(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "core.hooksPath", "/dev/null")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("commit", "--allow-empty", "-qm", "baseline")
    return git


def _units(tmp_path, *identities):
    (tmp_path / "MAINTENANCE.md").write_text("# root contract\n")
    unit_dir = tmp_path / "maintenance"
    unit_dir.mkdir(exist_ok=True)
    (unit_dir / "unit.md").write_text("".join(f"- Fork patch identity: `{i}`\n" for i in identities))


def test_commits_above_the_floor_need_trailers_owned_by_a_unit(tmp_path, monkeypatch):
    git = _repo(tmp_path)
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "pre-contract patch without a trailer")
    floor = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "new patch\n\nFork-Patch: fixture; upstream-pr: none")
    git("commit", "--allow-empty", "-qm", "evidence\n\nFork-Patch: evidence; retirement: not a patch")
    _units(tmp_path, "fixture")
    checker = _checker(tmp_path, monkeypatch)
    assert checker.check_trailers(base, floor) == []
    # Without a floor the pre-contract commit is unclassified.
    assert any("pre-contract patch" in f for f in checker.check_trailers(base))
    git("commit", "--allow-empty", "-qm", "unclassified patch")
    failures = checker.check_trailers(base, floor)
    assert len(failures) == 1 and "unclassified patch" in failures[0]


def test_trailer_identity_without_a_unit_owner_fails(tmp_path, monkeypatch):
    git = _repo(tmp_path)
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "orphan patch\n\nFork-Patch: orphan-slice; upstream-pr: none")
    git("commit", "--allow-empty", "-qm", "orphan follow-up\n\nFork-Patch: orphan-slice")
    _units(tmp_path, "fixture")
    checker = _checker(tmp_path, monkeypatch)
    failures = checker.check_trailers(base)
    assert len(failures) == 1  # one identity, reported once
    assert "orphan-slice" in failures[0] and "not owned" in failures[0]


def test_missing_maintenance_units_fail_closed(tmp_path, monkeypatch):
    git = _repo(tmp_path)
    (tmp_path / "FORK_PATCHES.md").write_text("obsolete ledger")
    checker = _checker(tmp_path, monkeypatch)
    failures = checker.check_trailers(git("rev-parse", "HEAD"))
    assert failures and "missing" in failures[0]
