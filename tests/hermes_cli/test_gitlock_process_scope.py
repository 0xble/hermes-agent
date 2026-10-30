"""Holder-scoped packs and conservative repository-scoped Linux lock cleanup."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import _git_process_scope as scopeprobe
from hermes_cli import gitlock


def _repo(root: Path) -> Path:
    (root / ".git" / "objects" / "pack").mkdir(parents=True)
    (root / ".git" / "refs").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return root


def _process(proc_root: Path, pid: int, cwd: Path, *, name: str = "git",
             argv: tuple[str, ...] = ("git", "fetch", "origin"), env: tuple[str, ...] = ()) -> Path:
    proc = proc_root / str(pid)
    proc.mkdir(parents=True)
    (proc / "comm").write_text(name + "\n")
    (proc / "cwd").symlink_to(cwd, target_is_directory=True)
    (proc / "cmdline").write_bytes(b"\0".join(os.fsencode(x) for x in argv) + b"\0")
    (proc / "environ").write_bytes(b"\0".join(os.fsencode(x) for x in env) + b"\0")
    (proc / "fd").mkdir()
    (proc / "maps").write_text("")
    return proc


@pytest.fixture
def scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    monkeypatch.setattr(scopeprobe, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(scopeprobe, "_linux_process_scope", True)
    # Static fake /proc never changes between scans; retrying a persistent veto only adds time.
    monkeypatch.setattr(scopeprobe, "INSPECTION_RETRY_SECONDS", 0.0)
    return proc_root, _repo(tmp_path / "ours"), _repo(tmp_path / "other")


def _aged(path: Path) -> Path:
    path.write_bytes(b"abandoned")
    stamp = time.time() - 3600
    os.utime(path, (stamp, stamp))
    return path


def test_unrelated_git_does_not_veto_tmp_pack_cleanup(scope):
    table, ours, other = scope
    _process(table, 123, other)
    pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")

    assert gitlock.clear_stale_tmp_packs(ours) == [str(pack)]
    assert not pack.exists()


def test_target_git_still_protects_tmp_pack(scope):
    table, ours, _ = scope
    _process(table, 123, ours)
    pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_live")

    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert pack.exists()


@pytest.mark.parametrize("state,threads,swept", [
    ("Z (zombie)", "1", True),     # exited Git awaiting reap: no cwd/environ/fd, holds nothing
    ("R (running)", "1", False),   # live but uninspectable: unknown ownership keeps the veto
    ("Z (zombie)", "3", False),    # zombie leader with live threads is still running
])
def test_exited_git_zombie_does_not_veto_but_live_uninspectable_does(scope, monkeypatch, state, threads, swept):
    """Linux nightly 38689220608: concurrent workers' Git children exit between our comm read and
    the cwd/environ read; each such zombie used to veto one candidate and leave it behind."""
    table, ours, _ = scope
    proc = _process(table, 321, ours)
    (proc / "cwd").unlink()          # the kernel drops cwd/environ/fd once a process exits
    (proc / "environ").unlink()
    (proc / "status").write_text(f"Name:\tgit\nState:\t{state}\nThreads:\t{threads}\n")
    pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")

    assert gitlock.clear_stale_tmp_packs(ours) == ([str(pack)] if swept else [])
    assert pack.exists() is not swept


def test_transient_proc_inspection_is_retried_before_veto(scope, monkeypatch):
    _, ours, _ = scope
    pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    sequence = iter((scopeprobe._INSPECTION_FAILED, False))
    monkeypatch.setattr(scopeprobe, "_scan_pack_owners", lambda repo, path: next(sequence))
    clock = iter((0.0, 0.1))
    monkeypatch.setattr(scopeprobe.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(scopeprobe.time, "sleep", lambda _: None)

    assert scopeprobe.tmp_pack_in_use(ours, pack, lambda: True, retry_until=1.0) is False

def test_stale_locks_keep_the_global_process_guard(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(scopeprobe, "_linux_process_scope", False)
    repo = _repo(tmp_path / "repo")
    lock = _aged(repo / ".git" / "index.lock")
    monkeypatch.setattr(gitlock, "_git_proc_running", lambda: True)

    assert gitlock.clear_stale_git_locks(repo) == []
    assert lock.exists()


@pytest.mark.parametrize("owner", ["foreign", "target", "linked", "unknown", "redirected", "unplaced"])
def test_index_lock_scope_preserves_possible_owners(scope, owner):
    table, ours, other = scope
    cwd = other if owner in ("foreign", "redirected", "unplaced") else ours
    if owner == "linked":
        cwd = ours.parent / "linked"
        cwd.mkdir()
        metadata = ours / ".git" / "worktrees" / "linked"
        metadata.mkdir(parents=True)
        (metadata / "commondir").write_text("../..")
        (cwd / ".git").write_text(f"gitdir: {metadata}\n")
    env = (f"GIT_INDEX_FILE={ours / '.git' / 'index'}",) if owner == "redirected" else ()
    argv = ("git", "-C", str(other), "fetch") if owner == "unplaced" else ("git", "fetch")
    proc = _process(table, 123, cwd, argv=argv, env=env)
    if owner == "unknown":
        (proc / "environ").unlink()
    lock = _aged(ours / ".git" / "index.lock")

    removed = gitlock.clear_stale_git_locks(ours)
    assert removed == ([str(lock)] if owner == "foreign" else [])
    assert lock.exists() is (owner != "foreign")


def test_fresh_index_lock_is_never_swept(scope):
    table, ours, other = scope
    _process(table, 123, other)
    lock = ours / ".git" / "index.lock"
    lock.write_bytes(b"active")

    assert gitlock.clear_stale_git_locks(ours) == []
    assert lock.read_bytes() == b"active"
