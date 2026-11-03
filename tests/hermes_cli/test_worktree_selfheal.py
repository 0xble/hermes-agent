"""Tests for git self-heal: atomic worktree-add failure cleanup + pack maintenance.

Regression for the Aug 2026 `hermes -w` timeout incident: 39 accumulated packs
slowed object lookups until `git worktree add` blew its 30s timeout, and the
timed-out add left a partially-materialized worktree plus a LOCKED admin entry
(lock pid = the live hermes process), poisoning every retry.

Two behaviors:
1. `_cleanup_failed_worktree_add` — removes the partial dir, the admin entry
   (even when LOCKED), and the orphaned branch, so a failed add is atomic.
2. `_maintain_pack_health` — repacks when *.pack count reaches the sprawl
   threshold; no-op below it.
"""

import os
import shlex
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from tests.waiting import wait_until

import pytest


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


@contextmanager
def _in_progress_add(repo):
    """Pause a real checkout in its smudge filter, while git holds index.lock."""
    marker = repo.parent / "checkout-started"
    slow_filter = repo.parent / "slow_filter.py"
    slow_filter.write_text(
        "import pathlib, sys, threading\n"
        f"pathlib.Path({str(marker)!r}).touch()\n"
        "threading.Event().wait()\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("f.txt filter=slow\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "slow checkout")
    wt = repo / ".worktrees" / "hermes-dead00"
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(slow_filter))}"
    proc = subprocess.Popen(
        ["git", "-c", f"filter.slow.smudge={command}", "worktree", "add", str(wt),
         "-b", "hermes/hermes-dead00"], cwd=repo, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    admin = repo / ".git" / "worktrees" / wt.name
    try:
        wait_until(marker.exists, timeout=20, what="git checkout smudge filter")
        assert (admin / "locked").read_text().strip() == "initializing"
        assert (admin / "index.lock").exists()
        yield wt, admin, proc
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


@pytest.mark.platforms("posix")
class TestCleanupFailedWorktreeAdd:
    def _simulate_timed_out_add(self, repo):
        with _in_progress_add(repo) as (wt, _admin, _proc):
            pass  # SIGKILL, not a synthetic Hermes lock or a post-checkout hook.
        return wt

    def test_sweeps_dir_admin_entry_and_branch(self, repo):
        from hermes_cli.worktree_ops import _cleanup_failed_worktree_add

        wt = self._simulate_timed_out_add(repo)
        admin = repo / ".git" / "worktrees" / "hermes-dead00"
        assert admin.exists() and (admin / "locked").exists()

        _cleanup_failed_worktree_add(str(repo), wt, "hermes/hermes-dead00")

        assert not wt.exists(), "partial worktree dir must be removed"
        assert not admin.exists(), "LOCKED admin entry must be removed"
        branches = _git(repo, "branch", "--list", "hermes/hermes-dead00").stdout
        assert branches.strip() == "", "orphaned branch must be deleted"

    def test_retry_succeeds_after_cleanup(self, repo):
        """The whole point: the same worktree name is creatable again."""
        from hermes_cli.worktree_ops import _cleanup_failed_worktree_add

        wt = self._simulate_timed_out_add(repo)
        _cleanup_failed_worktree_add(str(repo), wt, "hermes/hermes-dead00")

        result = _git(
            repo, "worktree", "add", str(wt), "-b", "hermes/hermes-dead00", check=False
        )
        assert result.returncode == 0, f"retry failed: {result.stderr}"

    def test_abandoned_initializing_add_is_swept_only_when_idle_and_old(self, repo):
        from hermes_cli.worktree_sweep import execute_plan, plan_sweep

        settings = {"archive_dirty": True, "archive_max_file_mb": 50,
                    "archive_retention_days": 30, "max_total_gb": 0}

        def age(wt, admin):
            stamp = time.time() - 100 * 3600
            for item in [*wt.rglob("*"), *admin.rglob("*"), wt]:
                os.utime(item, (stamp, stamp))

        with _in_progress_add(repo) as (wt, admin, _proc):
            age(wt, admin)
            execute_plan(str(repo), plan_sweep(str(repo), settings=settings), settings)
            assert wt.exists(), "even an aged initializing tree with live git must survive"
            assert (admin / "locked").read_text().strip() == "initializing"

        # The process is dead, but the ordinary young-tree gate still applies.
        wt.touch()
        execute_plan(str(repo), plan_sweep(str(repo), settings=settings), settings)
        assert wt.exists(), "a fresh initializing tree must survive"

        age(wt, admin)
        (admin / "locked").touch()
        execute_plan(str(repo), plan_sweep(str(repo), settings=settings), settings)
        assert wt.exists(), "a freshly restarted add in an old directory must survive"

        age(wt, admin)
        present = {str(p.relative_to(wt)): p.read_text() for p in wt.rglob("*")
                   if p.is_file() and p.name != ".git"}
        execute_plan(str(repo), plan_sweep(str(repo), settings=settings), settings)
        assert not wt.exists(), "an aged abandoned git initializing tree must be reclaimed"
        assert not admin.exists()
        assert not _git(repo, "branch", "--list", "hermes/hermes-dead00").stdout.strip()
        ref = "refs/hermes/archive/hermes-dead00"
        for name, contents in present.items():
            assert _git(repo, "show", f"{ref}:{name}").stdout == contents
        assert not _git(repo, "ls-tree", f"{ref}^2").stdout, "missing index must archive as empty"

    def test_noop_when_nothing_exists(self, repo):
        """Fail-soft on an error path where git never created anything."""
        from hermes_cli.worktree_ops import _cleanup_failed_worktree_add

        _cleanup_failed_worktree_add(
            str(repo), repo / ".worktrees" / "never-existed", "hermes/never-existed"
        )  # must not raise


class TestMaintainPackHealth:
    def _pack_count(self, repo):
        return len(list((repo / ".git" / "objects" / "pack").glob("*.pack")))

    def _make_packs(self, repo, n):
        """Create exactly n distinct packs via git pack-objects (deterministic
        across git versions — incremental `git repack` consolidates small
        packs on newer CI git builds, which made the count nondeterministic)."""
        pack_dir = repo / ".git" / "objects" / "pack"
        pack_dir.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (repo / f"p{i}.txt").write_text(f"{i}\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", f"c{i}")
            sha = _git(repo, "rev-parse", "HEAD^{commit}").stdout.strip()
            # One pack per commit object: pipe the sha into pack-objects.
            subprocess.run(
                ["git", "pack-objects", "-q", str(pack_dir / f"tpack{i}")],
                input=f"{sha}\n", cwd=str(repo), capture_output=True, text=True, check=True,
            )
        return self._pack_count(repo)

    def test_repacks_at_threshold(self, repo, monkeypatch):
        import cli
        from hermes_cli import worktree_ops

        made = self._make_packs(repo, 12)
        # Behavior contract, not a snapshot: the geometric repack leaves a size progression
        # (plus a cruft pack on newer git), so the exact count varies by git build. What must
        # hold: sprawl went DOWN and lookups now go through one multi-pack-index.
        threshold = 2
        monkeypatch.setattr(worktree_ops, "_PACK_SPRAWL_THRESHOLD", threshold)
        assert made > threshold, f"fixture failed to produce sprawl (made={made})"

        cli._maintain_pack_health(str(repo))

        after = self._pack_count(repo)
        assert after < made, f"pack count must strictly decrease (made={made}, after={after})"
        assert (repo / ".git" / "objects" / "pack" / "multi-pack-index").exists()

    def test_noop_below_threshold(self, repo, monkeypatch):
        import cli
        from hermes_cli import worktree_ops

        made = self._make_packs(repo, 2)
        monkeypatch.setattr(worktree_ops, "_PACK_SPRAWL_THRESHOLD", 50)

        cli._maintain_pack_health(str(repo))

        assert self._pack_count(repo) == made, "below threshold must be a no-op"

    def test_fail_soft_on_missing_pack_dir(self, tmp_path):
        from cli import _maintain_pack_health

        _maintain_pack_health(str(tmp_path / "not-a-repo"))  # must not raise


class TestRepackStampede:
    """Regression for the Sep 2026 shared-clone incident: every ``hermes -w`` launch started its
    own full repack, and a timed-out repack left ``pack-objects`` running for days."""

    def test_one_repack_per_clone_per_interval(self, repo, monkeypatch):
        from hermes_cli import worktree_ops

        monkeypatch.setattr(worktree_ops, "_PACK_SPRAWL_THRESHOLD", 0)
        runs: list = []
        monkeypatch.setattr(worktree_ops, "_run_bounded_repack", lambda root: runs.append(root))

        for _ in range(3):  # three concurrent-ish launches sharing the clone
            worktree_ops._maintain_pack_health(str(repo))
        assert runs == [str(repo)], "N launches inside the interval must produce exactly one repack"

        # A stale stamp (older than the interval) hands the slot to the next launch.
        monkeypatch.setattr(worktree_ops, "_REPACK_MIN_INTERVAL", 0)
        worktree_ops._maintain_pack_health(str(repo))
        assert len(runs) == 2

    @pytest.mark.platforms("linux")
    def test_timeout_kills_the_whole_repack_tree(self, tmp_path, monkeypatch):
        import os
        import time

        from hermes_cli import worktree_ops

        # A stand-in ``git`` that forks a long-lived grandchild, the way repack forks pack-objects.
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        pidfile = tmp_path / "grandchild.pid"
        shim = shim_dir / "git"
        shim.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > {pidfile}\nwait\n")
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(worktree_ops, "_REPACK_TIMEOUT", 1)

        worktree_ops._run_bounded_repack(str(tmp_path))

        grandchild = int(pidfile.read_text().strip())
        deadline = time.time() + 5
        while time.time() < deadline:
            if not Path(f"/proc/{grandchild}").exists():
                return
            time.sleep(0.05)
        subprocess.run(["kill", "-9", str(grandchild)], check=False)
        pytest.fail("pack-objects stand-in survived the repack timeout")
