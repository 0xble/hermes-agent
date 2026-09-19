"""Exercise commit classification through the maintenance support register."""

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


def test_support_register_classifies_legacy_commits(tmp_path, monkeypatch):
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
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "legacy patch")
    legacy = git("rev-parse", "HEAD")
    support = tmp_path / "maintenance" / "fork-patches.md"
    support.parent.mkdir()
    support.write_text(f"| `{legacy[:12]}` | legacy patch |\n")
    git("commit", "--allow-empty", "-qm", "new patch\n\nFork-Patch: fixture")
    checker = _checker(tmp_path, monkeypatch)
    assert checker.check_ledger(base) == []
    git("commit", "--allow-empty", "-qm", "unclassified patch")
    failures = checker.check_ledger(base)
    assert len(failures) == 1
    assert "unclassified patch" in failures[0]


def test_missing_support_register_fails_closed(tmp_path, monkeypatch):
    (tmp_path / "FORK_PATCHES.md").write_text("obsolete ledger")
    checker = _checker(tmp_path, monkeypatch)
    assert checker.check_ledger("HEAD") == ["maintenance/fork-patches.md is missing"]
