"""Exercise fork-patch commit classification: trailers above the floor, unit ownership of identities."""

import importlib.util
from pathlib import Path
import subprocess


def _checker(repo, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_fork_patches.py"
    spec = importlib.util.spec_from_file_location("fork_patch_checker", path)
    assert spec is not None and spec.loader is not None
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


def _units(tmp_path, *identities, prose=""):
    (tmp_path / "MAINTENANCE.md").write_text("# root contract\n")
    unit_dir = tmp_path / "maintenance"
    unit_dir.mkdir(exist_ok=True)
    body = "".join(f"- Fork patch identity: `{i}`\n" for i in identities)
    (unit_dir / "unit.md").write_text(body + prose)


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


def test_every_trailer_on_a_multi_trailer_commit_is_checked(tmp_path, monkeypatch):
    git = _repo(tmp_path)
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "squash\n\nFork-Patch: owned\nFork-Patch: never-documented")
    _units(tmp_path, "owned")
    checker = _checker(tmp_path, monkeypatch)
    failures = checker.check_trailers(base)
    assert len(failures) == 1 and "never-documented" in failures[0]


def test_ownership_requires_the_exact_backticked_token(tmp_path, monkeypatch):
    """Prose containing the identity as a substring, or a longer backticked token, does not own it."""
    git = _repo(tmp_path)
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "a\n\nFork-Patch: slice")
    git("commit", "--allow-empty", "-qm", "b\n\nFork-Patch: e")
    git("commit", "--allow-empty", "-qm", "c\n\nFork-Patch: slice-9-vault")
    _units(tmp_path, "slice-9-vault-camofox", prose="The slice is maintained here; see the evidence.\n")
    checker = _checker(tmp_path, monkeypatch)
    failures = checker.check_trailers(base)
    assert sorted(f.split("'")[1] for f in failures) == ["e", "slice", "slice-9-vault"]


def test_blank_trailer_is_not_a_classification(tmp_path, monkeypatch):
    git = _repo(tmp_path)
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "blank\n\nFork-Patch: ")
    _units(tmp_path, "fixture")
    checker = _checker(tmp_path, monkeypatch)
    failures = checker.check_trailers(base)
    assert len(failures) == 1 and "no Fork-Patch trailer" in failures[0]


def test_rewritten_floor_sha_is_located_by_subject_or_fails_clearly(tmp_path, monkeypatch):
    git = _repo(tmp_path)
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "pre-contract floor commit")
    git("commit", "--allow-empty", "-qm", "later\n\nFork-Patch: fixture")
    _units(tmp_path, "fixture")
    checker = _checker(tmp_path, monkeypatch)
    gone = "deadbeef" * 5
    # Subject fallback: the floor is found even though the SHA no longer exists.
    assert checker.check_trailers(base, gone, "pre-contract floor commit") == []
    # No subject match: one clear failure, no traceback.
    failures = checker.check_trailers(base, gone, "not a subject in this history")
    assert len(failures) == 1 and "does not resolve" in failures[0]
    failures = checker.check_trailers(base, gone)
    assert len(failures) == 1 and "does not resolve" in failures[0]


def test_missing_maintenance_units_fail_closed(tmp_path, monkeypatch):
    git = _repo(tmp_path)
    (tmp_path / "FORK_PATCHES.md").write_text("obsolete ledger")
    checker = _checker(tmp_path, monkeypatch)
    failures = checker.check_trailers(git("rev-parse", "HEAD"))
    assert failures and "missing" in failures[0]
