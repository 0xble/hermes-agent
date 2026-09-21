"""Exercise source sync against real local remotes, including the scheduled recovery path."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.mark.parametrize("new_release", [False, True])
def test_sync_refreshes_tests_and_publishes_without_promoting(tmp_path, monkeypatch, new_release):
    spec = importlib.util.spec_from_file_location("sync", Path(__file__).parents[2] / "scripts/sync_fork_candidate.py")
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    git(upstream, "config", "user.email", "test@example.invalid")
    git(upstream, "config", "user.name", "Test")
    git(upstream, "config", "core.hooksPath", "/dev/null")
    (upstream / "base").write_text("base")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "baseline")
    git(upstream, "tag", "v2026.9.14")
    fork = tmp_path / "fork"
    git(tmp_path, "clone", str(upstream), str(fork))
    for key, value in [("user.email", "test@example.invalid"), ("user.name", "Test"), ("core.hooksPath", "/dev/null")]:
        git(fork, "config", key, value)
    # A fork-only newer tag must not become the upstream release target.
    git(fork, "tag", "v2099.1.1")
    source = tmp_path / "source"
    git(tmp_path, "clone", str(fork), str(source))
    git(source, "remote", "add", "upstream-live", str(upstream))
    git(source, "config", "core.hooksPath", "/dev/null")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "user.name", "Test")
    # Advance the fork after cloning: relying on cached origin/main loses this patch.
    (fork / "patch").write_text("preserve me")
    git(fork, "add", ".")
    git(fork, "commit", "-m", "fork patch")
    fork_head = git(fork, "rev-parse", "HEAD")
    if new_release:
        (upstream / "release").write_text("new release")
        git(upstream, "add", ".")
        git(upstream, "commit", "-m", "next release")
        git(upstream, "tag", "v2026.9.21")
    expected_tag = "v2026.9.21" if new_release else "v2026.9.14"
    target = source / ".worktrees/sync"
    observed = []
    def verify(repo, **kwargs):
        observed.append(git(repo, "rev-parse", "HEAD"))
        assert (repo / "patch").read_text() == "preserve me"
        return {"exit": 0, "summary": "real candidate observed"}
    monkeypatch.setattr(sync, "verify_candidate", verify)
    receipt = tmp_path / "result.json"
    args = ["--repo", str(target), "--source-repo", str(source), "--candidate", "origin/main",
            "--publish", "--verify-current", "--result", str(receipt)]
    assert sync.main(args) == 0
    result = json.loads(receipt.read_text())
    assert result["newest_tag"] == expected_tag
    assert result["candidate"] == fork_head
    assert result["tests"]["exit"] == 0
    assert result["published"]["sha"] == observed[0]
    assert git(fork, "rev-parse", "HEAD") == fork_head  # candidate publication never promotes main
    assert git(source, "rev-parse", "HEAD") != fork_head  # preserve the coordination checkout
    assert git(target, "merge-base", "--is-ancestor", result["tag_sha"], observed[0]) == ""
    # Repeated scheduled runs reuse the dedicated worktree safely.
    assert sync.main(args) == 0
    published = git(fork, "rev-parse", f"refs/heads/candidate/{expected_tag}")
    monkeypatch.setattr(sync, "verify_candidate", lambda *a, **k: {"exit": 1, "summary": "regression"})
    assert sync.main(args) == 1
    assert json.loads(receipt.read_text())["status"] == "tests_failed"
    assert git(fork, "rev-parse", f"refs/heads/candidate/{expected_tag}") == published
    # Protected dirt is a failure, never an apparently successful no-op.
    (target / "unowned").write_text("leave me")
    assert sync.main(args) == 1
    assert (target / "unowned").read_text() == "leave me"


def test_failed_verification_never_publishes(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("sync", Path(__file__).parents[2] / "scripts/sync_fork_candidate.py")
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    monkeypatch.setattr(sync, "FORK_TESTS", ["tests/test_boundary.py"])
    missing = sync.verify_candidate(tmp_path)
    assert missing["exit"] == 1 and "tests/test_boundary.py" in missing["output"]
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_boundary.py").write_text("def test_contract(): pass\n")
    # Runner failures must remain failures, with useful stderr in the receipt.
    (tmp_path / "scripts").mkdir()
    runner = tmp_path / "scripts/run_tests.sh"
    runner.write_text("#!/bin/sh\necho regression >&2\nexit 7\n")
    runner.chmod(0o700)
    result = sync.verify_candidate(tmp_path)
    assert result["exit"] == 7 and "regression" in result["output"]
