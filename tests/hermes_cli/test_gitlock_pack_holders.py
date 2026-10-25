"""Aborted packs heal amid unplaced parallel Git; locks still require ownership proof."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

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


@pytest.mark.platforms("posix")
def test_unreadable_foreign_non_git_processes_do_not_veto(scope, monkeypatch):
    """A non-root sweep cannot read root's or another user's /proc fd/maps. Those processes
    cannot write the pack directory, so they cannot be its live writer (review of #479)."""
    table, ours, _ = scope
    proc = _process(table, 1, Path("/"), name="systemd", argv=("/sbin/init",))
    pack_dir = ours / ".git" / "objects" / "pack"
    pack_dir.chmod(0o755)
    pack = _aged(pack_dir / "tmp_pack_dead")
    original_iterdir, original_stat = type(pack).iterdir, type(pack).stat
    foreign = os.getuid() + 1 if os.getuid() else 1

    class _Stat:
        st_uid = foreign

    def denied(path):
        if path == proc / "fd":
            raise PermissionError(path)
        return original_iterdir(path)

    def foreign_owner(path, *args, **kwargs):
        return _Stat() if path == proc else original_stat(path, *args, **kwargs)

    monkeypatch.setattr(type(pack), "iterdir", denied)
    monkeypatch.setattr(type(pack), "stat", foreign_owner)
    assert gitlock.clear_stale_tmp_packs(ours) == [str(pack)]
    assert not pack.exists()

    # Our own uid, or a Git of any uid, stays a veto when it cannot be inspected.
    pack = _aged(pack_dir / "tmp_pack_dead")
    _Stat.st_uid = os.getuid()
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert pack.exists()
    _Stat.st_uid = foreign
    (proc / "comm").write_text("git\n")
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


def test_unplaced_git_through_a_symlink_to_the_target_keeps_its_pack(scope, tmp_path):
    table, ours, other = scope
    alias = tmp_path / "alias-to-ours"
    alias.symlink_to(ours, target_is_directory=True)
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / "rel-alias").symlink_to(ours, target_is_directory=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "alias").symlink_to(ours, target_is_directory=True)
    pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_live")
    for pid, argv in enumerate([("git", "-C", str(alias), "fetch"), ("git", "-C", "rel-alias", "fetch"),
                                ("git", "upload-pack", "--stateless-rpc", "rel-alias"),
                                ("git", "-C", str(external), "-C", "alias", "fetch"),
                                ("git", f"-C{external}", "-Calias", "fetch")], start=200):
        proc = _process(table, pid, launch, argv=argv)
        assert gitlock.clear_stale_tmp_packs(ours) == []
        assert pack.exists()
        shutil.rmtree(proc)


def _linked_worktree(main: Path, name: str, root: Path) -> tuple[Path, Path]:
    """Mirror `git worktree add`: metadata under main/.git/worktrees, a gitfile in the tree."""
    meta = main / ".git" / "worktrees" / name
    meta.mkdir(parents=True)
    (meta / "commondir").write_text("../..\n")
    (meta / "HEAD").write_text("ref: refs/heads/wt\n")
    tree = root / name
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {meta}\n")
    return tree, meta


def test_env_real_git_exports_for_foreign_work_does_not_veto(scope, tmp_path):
    """Git exports GIT_INDEX_FILE for an ordinary commit and an absolute GIT_DIR for
    `git -C <linked worktree>`. Neither proves the target is involved."""
    table, ours, other = scope
    launch = tmp_path / "launch"
    launch.mkdir()
    tree, meta = _linked_worktree(other, "wt", tmp_path)
    _process(table, 300, other, argv=("git", "commit", "-m", "x"), env=("GIT_INDEX_FILE=.git/index",))
    _process(table, 301, tree, argv=("git", "-C", str(tree), "commit", "-m", "x"),
             env=(f"GIT_DIR={meta}", f"GIT_INDEX_FILE={meta / 'index'}"))
    _process(table, 302, launch, argv=("git", "-C", str(tree), "fetch"), env=(f"GIT_DIR={meta}",))
    pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    assert gitlock.clear_stale_tmp_packs(ours) == [str(pack)]
    assert not pack.exists()


def test_absolute_git_dir_of_a_target_worktree_keeps_its_pack(scope, tmp_path):
    """A linked worktree of the target shares its object store, so its Git owns the pack."""
    table, ours, _ = scope
    launch = tmp_path / "launch"
    launch.mkdir()
    tree, meta = _linked_worktree(ours, "wt", tmp_path)
    _process(table, 310, launch, argv=("git", "-C", str(tree), "fetch"), env=(f"GIT_DIR={meta}",))
    pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_live")
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert pack.exists()


def test_unresolvable_explicit_git_dir_is_unknown_not_unrelated(scope, tmp_path):
    """Git with core.worktree chdirs into the work tree after reading a relative --git-dir, so
    the argv path no longer resolves from /proc cwd. That is unknown ownership, so the pack stays."""
    table, ours, _ = scope
    worktree = tmp_path / "configured-worktree"
    worktree.mkdir()
    for pid, (argv, env) in enumerate([
        (("git", "--git-dir=ours/.git", "status"), ("GIT_DIR=.git",)),
        (("git", "--git-dir", "missing/.git", "fetch"), ()),
        (("git", "status"), ("GIT_DIR=does-not-exist",)),
    ], start=400):
        proc = _process(table, pid, worktree, argv=argv, env=env)
        pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_live")
        assert gitlock.clear_stale_tmp_packs(ours) == [], argv
        assert pack.exists()
        shutil.rmtree(proc)


def test_replaced_or_touched_pack_is_not_deleted_after_holder_proof(scope, monkeypatch):
    _, ours, _ = scope
    pack = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    def replaced(repo, path, fallback, **kwargs):
        path.rename(path.with_name("held-after-rename"))
        path.write_bytes(b"new live transfer")
        return False
    monkeypatch.setattr(scopeprobe, "tmp_pack_in_use", replaced, raising=False)
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert pack.read_bytes() == b"new live transfer"
