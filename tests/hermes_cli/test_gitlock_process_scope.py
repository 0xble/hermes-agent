"""Holder-scoped Linux tmp-pack cleanup with conservative global lock cleanup."""
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


def test_stale_locks_keep_the_global_process_guard(monkeypatch, tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    lock = _aged(repo / ".git" / "index.lock")
    monkeypatch.setattr(gitlock, "_git_proc_running", lambda: True)

    assert gitlock.clear_stale_git_locks(repo) == []
    assert lock.exists()
