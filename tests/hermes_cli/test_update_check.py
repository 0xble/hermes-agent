"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def test_passive_check_reads_only_configured_fork_origin(tmp_path):
    """A real checkout learns a fork update without an upstream remote or fallback."""
    from hermes_cli import banner

    remote = tmp_path / "brian-fork.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"
    _git(["init", "--bare", "-b", "main", str(remote)], tmp_path)
    _git(["init", "-b", "main", str(seed)], tmp_path)
    _git(["config", "user.email", "test@example.com"], seed)
    _git(["config", "user.name", "Test User"], seed)
    (seed / "tracked.txt").write_text("first\n", encoding="utf-8")
    _git(["add", "tracked.txt"], seed)
    _git(["commit", "-m", "initial"], seed)
    _git(["remote", "add", "origin", str(remote)], seed)
    _git(["push", "origin", "main"], seed)
    _git(["clone", str(remote), str(checkout)], tmp_path)

    (seed / "tracked.txt").write_text("second\n", encoding="utf-8")
    _git(["commit", "-am", "fork release"], seed)
    _git(["push", "origin", "main"], seed)

    assert _git(["remote"], checkout).splitlines() == ["origin"]
    assert banner._check_via_local_git(checkout) == 1
    assert _git(["remote"], checkout).splitlines() == ["origin"]




def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """When cache is fresh, check_for_updates should return cached value without calling git."""
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    # Create a fake git repo and fresh cache
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(
        json.dumps({
            "ts": time.time(), "behind": 3, "ver": __version__,
            "source": "git@github.com:0xble/hermes-agent.git",
        }),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.banner.subprocess.run") as mock_run:
        result = check_for_updates()

    assert result == 3
    mock_run.assert_not_called()






def test_prefetch_non_blocking():
    """prefetch_update_check() should return immediately without blocking."""
    import hermes_cli.banner as banner

    # Reset module state
    banner._update_result = None
    banner._update_check_done = threading.Event()

    with patch.object(banner, "check_for_updates", return_value=5):
        start = time.monotonic()
        banner.prefetch_update_check()
        elapsed = time.monotonic() - start

        # Should return almost immediately (well under 1 second)
        assert elapsed < 1.0

        # Wait for the background thread to finish
        banner._update_check_done.wait(timeout=5)
        assert banner._update_result == 5


def test_fork_main_sha_disables_git_prompts(monkeypatch):
    """The passive fork probe must never inherit the interactive terminal."""
    from hermes_cli import banner

    completed = MagicMock(returncode=1, stdout="", stderr="auth required")
    run = MagicMock(return_value=completed)
    monkeypatch.setattr(banner.subprocess, "run", run)

    assert banner._upstream_main_sha() is None
    kwargs = run.call_args.kwargs
    assert kwargs["stdin"] is banner.subprocess.DEVNULL
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kwargs["env"]["GCM_INTERACTIVE"] == "Never"
    assert kwargs["env"]["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"


def test_check_via_local_git_fetch_failure_returns_none(tmp_path, monkeypatch):
    """When git fetch fails and the stale origin/main ref is not ahead,
    _check_via_local_git must return None (#82166).

    A stale tracking ref cannot prove *currentness* (rev-list 0 just means
    the ref hasn't caught up), so returning None is the honest inconclusive
    result — and the caller must not cache it as "up to date".
    """
    from hermes_cli import banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    # Simulate a non-shallow, non-SSH-remote checkout
    def mock_git_stdout(args, *, cwd, timeout=5, network=False):
        if args[:2] == ["remote", "get-url"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        return None

    # Fetch fails (returncode != 0); stale rev-list reports 0 behind
    failed_proc = MagicMock()
    failed_proc.returncode = 1
    failed_proc.stdout = ""
    failed_proc.stderr = "fatal: could not reach remote"

    stale_zero_proc = MagicMock()
    stale_zero_proc.returncode = 0
    stale_zero_proc.stdout = "0"

    fetch_kwargs = None

    def mock_run(args, **kwargs):
        nonlocal fetch_kwargs
        if args[:2] == ["git", "fetch"]:
            fetch_kwargs = kwargs
            return failed_proc
        if args[:2] == ["git", "rev-list"]:
            return stale_zero_proc
        raise AssertionError(f"unexpected subprocess.run: {args}")

    monkeypatch.setattr(banner, "_git_stdout", mock_git_stdout)
    monkeypatch.setattr(banner.subprocess, "run", mock_run)

    result = banner._check_via_local_git(repo_dir)
    assert result is None, (
        "Fetch failure with stale 0-behind must return None, not 'up to date'"
    )
    assert fetch_kwargs is not None
    assert fetch_kwargs["stdin"] is banner.subprocess.DEVNULL
    assert fetch_kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert fetch_kwargs["env"]["GCM_INTERACTIVE"] == "Never"


def test_check_via_local_git_fetch_failure_never_uses_stale_ref(tmp_path, monkeypatch):
    """A failed fork fetch is inconclusive; stale refs cannot authorize a notice."""
    from hermes_cli import banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    def mock_git_stdout(args, *, cwd, timeout=5, network=False):
        if args[:2] == ["remote", "get-url"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        return None

    failed_proc = MagicMock()
    failed_proc.returncode = 1
    failed_proc.stdout = ""
    failed_proc.stderr = "fatal: could not reach remote"

    def mock_run(args, **kwargs):
        if args[:2] == ["git", "fetch"]:
            return failed_proc
        raise AssertionError(f"unexpected subprocess.run: {args}")

    monkeypatch.setattr(banner, "_git_stdout", mock_git_stdout)
    monkeypatch.setattr(banner.subprocess, "run", mock_run)

    result = banner._check_via_local_git(repo_dir)
    assert result is None


def test_check_via_local_git_fetch_failure_does_not_run_rev_list(tmp_path, monkeypatch):
    """A failed fork fetch stops before any stale-ref comparison."""
    from hermes_cli import banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    def mock_git_stdout(args, *, cwd, timeout=5, network=False):
        if args[:2] == ["remote", "get-url"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        return None

    failed_proc = MagicMock()
    failed_proc.returncode = 1
    failed_proc.stdout = ""
    failed_proc.stderr = "fatal: could not reach remote"

    bad_rev_list = MagicMock()
    bad_rev_list.returncode = 128
    bad_rev_list.stdout = ""
    bad_rev_list.stderr = "fatal: ambiguous argument 'HEAD..origin/main'"

    def mock_run(args, **kwargs):
        if args[:2] == ["git", "fetch"]:
            return failed_proc
        if args[:2] == ["git", "rev-list"]:
            return bad_rev_list
        raise AssertionError(f"unexpected subprocess.run: {args}")

    monkeypatch.setattr(banner, "_git_stdout", mock_git_stdout)
    monkeypatch.setattr(banner.subprocess, "run", mock_run)

    result = banner._check_via_local_git(repo_dir)
    assert result is None


def test_check_for_updates_does_not_cache_none(tmp_path, monkeypatch):
    """check_for_updates must not cache None results so a transient fetch
    failure doesn't suppress retries for the full 6-hour cache window (#82166).

    Instead of mocking the full Path resolution chain, we verify the cache-write
    guard directly: call check_for_updates with a mocked _check_via_local_git
    that returns None, and confirm no cache file is created.
    """
    import hermes_cli.banner as banner

    cache_file = tmp_path / ".update_check"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_REVISION", raising=False)

    # Create a fake repo dir so the .git check passes
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    # Mock the internal functions to force the local-git path returning None
    monkeypatch.setattr(banner, "_check_via_local_git", lambda rd: None)
    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method", lambda root: "git"
    )
    monkeypatch.setattr(
        "hermes_cli.config.get_project_root", lambda: repo_dir
    )

    # Patch __file__ resolution by monkeypatching the module's Path calls.
    # check_for_updates does: Path(__file__).parent.parent.resolve()
    # We intercept by making the resolve() return our fake repo_dir.
    original_init = Path.__init__

    def patched_path_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

    # Simpler: just patch the get_hermes_home and the repo_dir resolution
    # by making check_for_updates find our fake repo via hermes_home fallback.
    # The code checks Path(__file__).parent.parent/.git first, then falls
    # back to hermes_home / "hermes-agent". We ensure the fallback hits.
    # To do this, we make Path(__file__).parent.parent.resolve() return
    # a path without .git, so it falls through to hermes_home / "hermes-agent".
    real_resolve = Path.resolve

    def fake_resolve(self, *args, **kwargs):
        s = str(self)
        if "banner.py" in s or s.endswith("hermes_cli"):
            # Return a path that has no .git, forcing the fallback
            return tmp_path / "no-git-here"
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    result = banner.check_for_updates()
    assert result is None

    # The cache file must NOT have been written with a None result
    assert not cache_file.exists(), "None result must not be cached"




