"""Aborted packs heal amid unplaced parallel Git; locks still require ownership proof."""
from __future__ import annotations

import os
import shutil

import pytest

from hermes_cli import _git_process_scope as scopeprobe
from hermes_cli import gitlock
from tests.hermes_cli.test_gitlock_process_scope import _aged, _process, scope


@pytest.mark.parametrize("argv", [
    ("git", "clone", "https://example.invalid/repo", "destination"),
    ("git", "-C", "..", "fetch"),
    ("git", "upload-pack", "--stateless-rpc", "."),
    ("git", "upload-pack", "--stateless-rpc", "--advertise-refs", "."),
])
@pytest.mark.parametrize("cwd_kind", ["metadata", "nonrepo"])
@pytest.mark.parametrize("holder", ["none", "fd", "hardlink", "mmap", "directory", "unreadable-fd", "unreadable-maps"])
def test_unplaced_parallel_git_only_keeps_held_or_unobservable_packs(scope, argv, holder, cwd_kind):
    table, ours, other = scope
    cwd = other / ".git"
    if cwd_kind == "nonrepo":
        cwd = other.parent / "launch"
        cwd.mkdir()
    proc = _process(table, 123, cwd, argv=argv)
    fd = proc / "fd"
    (proc / "maps").write_text("")
    pack = ours / ".git" / "objects" / "pack"
    dead = _aged(pack / "tmp_pack_dead")
    live = _aged(pack / "tmp_pack_live")
    young = pack / "tmp_pack_young"
    young.write_bytes(b"live")
    lock = _aged(ours / ".git" / "index.lock")
    if holder == "fd":
        (fd / "3").symlink_to(live)
    elif holder == "hardlink":
        alias = other / "held-pack"
        os.link(live, alias)
        (fd / "3").symlink_to(alias)
    elif holder == "mmap":
        st = live.stat()
        (proc / "maps").write_text(
            f"1000-2000 r--p 00000000 {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x} {st.st_ino} /hidden/alias\n")
    elif holder == "directory":
        (fd / "3").symlink_to(pack, target_is_directory=True)
    elif holder == "unreadable-fd":
        shutil.rmtree(fd)
    elif holder == "unreadable-maps":
        (proc / "maps").unlink()
    removed = gitlock.clear_stale_tmp_packs(ours)
    blocked = holder in {"directory", "unreadable-fd", "unreadable-maps"}
    expected = [] if blocked else [str(dead)] + ([str(live)] if holder == "none" else [])
    assert set(removed) == set(expected)
    assert dead.exists() is blocked
    assert live.exists() is (holder != "none")
    assert young.exists()
    assert gitlock.clear_stale_git_locks(ours) == []
    assert lock.exists()


@pytest.mark.parametrize("visibility", ["non-git-holder", "fd-disappears", "process-disappears", "proc-denied"])
def test_holder_visibility_and_disappearance_are_not_repository_ownership(scope, monkeypatch, visibility):
    table, ours, other = scope
    proc = _process(table, 123, other, name="python", argv=("python",))
    (proc / "maps").write_text("")
    pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    (proc / "fd" / "3").symlink_to(pack)
    original = type(table).iterdir
    stat = type(table).stat
    def disappearing_stat(path, *args, **kwargs):
        if path == proc / "fd" / "3":
            path.unlink()
            if visibility == "process-disappears":
                shutil.rmtree(proc)
            raise FileNotFoundError(path)
        return stat(path, *args, **kwargs)
    def denied(path):
        if path == table:
            raise PermissionError(path)
        return original(path)
    if visibility in {"fd-disappears", "process-disappears"}:
        monkeypatch.setattr(type(table), "stat", disappearing_stat)
    if visibility == "proc-denied":
        monkeypatch.setattr(type(table), "iterdir", denied)
    removable = visibility in {"fd-disappears", "process-disappears"}
    assert gitlock.clear_stale_tmp_packs(ours) == ([str(pack)] if removable else [])
    assert pack.exists() is not removable


@pytest.mark.parametrize("unreadable", ["fd-entry", "maps", "comm"])
def test_permission_errors_on_live_holder_visibility_keep_packs(scope, monkeypatch, unreadable):
    table, ours, other = scope
    proc = _process(table, 123, other, name="python", argv=("python",))
    pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    fd = proc / "fd" / "3"
    fd.symlink_to(pack)
    original_stat, original_read = type(pack).stat, type(pack).read_text
    def denied_stat(path, *args, **kwargs):
        if unreadable == "fd-entry" and path == fd:
            raise PermissionError(path)
        return original_stat(path, *args, **kwargs)
    def denied_read(path, *args, **kwargs):
        if path == proc / unreadable:
            raise PermissionError(path)
        return original_read(path, *args, **kwargs)
    # An unreadable map must veto even when its fd is no longer open.
    if unreadable == "maps":
        fd.unlink()
    monkeypatch.setattr(type(pack), "stat", denied_stat)
    monkeypatch.setattr(type(pack), "read_text", denied_read)
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert pack.exists()


def test_unplaced_git_still_accepts_exact_custody_config(scope):
    table, ours, other = scope
    _process(table, 123, other, argv=("git", "-C", "..", "-c", "maintenance.auto=false", "clone", "source", "target"))
    pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    assert gitlock.clear_stale_tmp_packs(ours) == [str(pack)]


@pytest.mark.parametrize("argv,env", [
    (("git", "-C", "..", "-c", "include.path=/unproved", "fetch"), ()),
    (("git", "clone", "-ccore.worktree=/unproved", "source", "target"), ()),
    (("git", "clone", "--config=include.path=/unproved", "source", "target"), ()),
    (("git", "-C", "..", "--git-dir=/unproved", "fetch"), ()),
    (("git", "clone", "source", "target"), ("GIT_DIR=.",)),
    (("git", "init", "target"), ()),
])
def test_unplaced_exception_does_not_bypass_unparsed_configuration_guards(scope, argv, env):
    table, ours, other = scope
    _process(table, 123, other, argv=argv, env=env)
    pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert pack.exists()


def test_replaced_or_touched_pack_is_not_deleted_after_holder_proof(scope, monkeypatch):
    _, ours, _ = scope
    pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    def replaced(repo, path, fallback):
        path.rename(path.with_name("held-after-rename"))
        path.write_bytes(b"new live transfer")
        return False
    monkeypatch.setattr(scopeprobe, "tmp_pack_in_use", replaced, raising=False)
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert pack.read_bytes() == b"new live transfer"
