"""Linux recovery must not confuse another checkout's transfer with our own."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import _git_process_scope as scopeprobe
from hermes_cli import gitlock


def _repo(root: Path) -> Path:
    (root / ".git" / "objects" / "pack").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return root


def _process(proc_root, pid, cwd, *, name="git", argv=("git", "fetch", "origin"), env=()):
    proc = proc_root / str(pid)
    proc.mkdir(parents=True)
    (proc / "comm").write_text(name + "\n")
    (proc / "cwd").symlink_to(cwd, target_is_directory=True)
    (proc / "cmdline").write_bytes(b"\0".join(os.fsencode(x) for x in argv) + b"\0")
    (proc / "environ").write_bytes(b"\0".join(os.fsencode(x) for x in env) + b"\0")
    return proc


@pytest.fixture
def scope(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    # The process table is the OS boundary; real files and sweeps stay unmocked.
    monkeypatch.setattr(scopeprobe, "_PROC_ROOT", root)
    monkeypatch.setattr(scopeprobe, "_linux_process_scope", True)
    return root, _repo(tmp_path / "ours"), _repo(tmp_path / "other")


def _aged(path):
    path.write_bytes(b"abandoned")
    stamp = time.time() - 3600
    os.utime(path, (stamp, stamp))
    return path


@pytest.mark.parametrize("resource", ["index.lock", "objects/pack/tmp_pack_dead"])
def test_unrelated_git_does_not_veto_stale_recovery(scope, resource):
    table, ours, other = scope
    _process(table, 123, other)
    debris = _aged(ours / ".git" / resource)
    sweep = gitlock.clear_stale_git_locks if resource == "index.lock" else gitlock.clear_stale_tmp_packs
    assert sweep(ours) == [str(debris)]
    assert not debris.exists()


@pytest.mark.parametrize("name", ["git", "git-index-pack", "git-pack-objects"])
def test_target_git_and_helpers_keep_old_resources(scope, name):
    table, ours, _ = scope
    _process(table, 123, ours, name=name, argv=(name,))
    lock = _aged(ours / ".git" / "index.lock")
    debris = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    assert gitlock.clear_stale_git_locks(ours) == []
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert lock.exists() and debris.exists()


@pytest.mark.parametrize("mode", ["cwd", "git-dir-env", "git-dir-arg", "chdir-arg", "object-dir-env"])
def test_explicit_target_repository_is_protected(scope, mode):
    table, ours, other = scope
    env = ()
    argv = ("git", "fetch")
    cwd = other
    if mode == "cwd":
        cwd = ours / ".git" / "objects"
    elif mode == "git-dir-env":
        env = (f"GIT_DIR={ours / '.git'}",)
    elif mode == "git-dir-arg":
        argv = ("git", f"--git-dir={ours / '.git'}", "fetch")
    elif mode == "chdir-arg":
        argv = ("git", "-C", str(ours), "fetch")
    else:
        env = (f"GIT_OBJECT_DIRECTORY={ours / '.git' / 'objects'}",)
    _process(table, 123, cwd, argv=argv, env=env)
    lock = _aged(ours / ".git" / "index.lock")
    assert gitlock.clear_stale_git_locks(ours) == []
    assert lock.exists()


@pytest.mark.parametrize("mode", ["index", "relative-chdir", "pack-output", "index-output", "inline-config"])
def test_redirected_resources_never_lose_the_live_writer_veto(scope, mode):
    table, ours, other = scope
    argv, env, cwd = ("git", "fetch"), (), other
    if mode == "index":
        env = (f"GIT_INDEX_FILE={ours / '.git' / 'index'}",)
    elif mode == "relative-chdir":
        # This is the observed cwd AFTER Git applied its relative -C. Replaying
        # it would resolve an outer repository instead of the actual owner.
        ours = _repo(other / "nested")
        cwd, argv = ours, ("git", "-C", "..", "fetch")
    elif mode == "pack-output":
        argv = ("git", "pack-objects", str(ours / ".git" / "objects" / "pack" / ".tmp-live"))
    elif mode == "index-output":
        argv = ("git", "index-pack", "--stdin", "-o", str(ours / ".git" / "objects" / "pack" / "tmp_live"))
    else:
        argv = ("git", "-ccore.worktree=" + str(ours), "fetch")
    _process(table, 123, cwd, argv=argv, env=env)
    lock = _aged(ours / ".git" / "index.lock")
    debris = _aged(ours / ".git" / "objects" / "pack" / ".tmp-live.pack")
    assert gitlock.clear_stale_git_locks(ours) == []
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert lock.exists() and debris.exists()


@pytest.mark.parametrize("source", ["argv", "argv-joined", "inherited"])
def test_updater_custody_config_does_not_hide_an_unrelated_repository(scope, source):
    # Every updater Git carries these flags; a concurrent update of another
    # checkout must not veto recovery here, or dead transfers' packs pile up.
    table, ours, other = scope
    flags = ("gc.autoDetach=false", "maintenance.auto=false", "core.fsmonitor=false", "credential.helper=",
             f"core.hooksPath={os.devnull}")
    argv, env = ("git", "fetch", "origin"), ()
    if source == "argv":
        argv = ("git", *[x for flag in flags for x in ("-c", flag)], "fetch", "origin")
    elif source == "argv-joined":
        argv = ("git", *[f"-c{flag}" for flag in flags], "fetch", "origin")
    else:
        argv = ("git-index-pack", "--stdin")
        env = ("GIT_CONFIG_PARAMETERS='gc.autodetach'='false' 'maintenance.auto'='false' 'credential.helper'=''",)
    _process(table, 123, other, argv=argv, env=env)
    debris = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    assert gitlock.clear_stale_tmp_packs(ours) == [str(debris)]


@pytest.mark.parametrize("source", ["argv", "inherited"])
def test_updater_custody_config_on_the_target_repository_still_vetoes(scope, source):
    table, ours, _ = scope
    argv, env = ("git", "-c", "gc.autoDetach=false", "fetch"), ()
    if source == "inherited":
        argv, env = ("git", "fetch"), ("GIT_CONFIG_PARAMETERS='maintenance.auto'='false'",)
    _process(table, 123, ours, argv=argv, env=env)
    debris = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert debris.exists()


@pytest.mark.parametrize("env", [
    "GIT_CONFIG_PARAMETERS='gc.autodetach'='false' 'core.worktree'='/elsewhere'",
    "GIT_CONFIG_PARAMETERS='unterminated",
])
def test_inherited_config_beyond_custody_keeps_the_veto(scope, env):
    table, ours, other = scope
    _process(table, 123, other, argv=("git", "fetch"), env=(env,))
    debris = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert debris.exists()


@pytest.mark.parametrize("store", ["objects", "objects/pack"])
def test_shared_symlinked_object_store_keeps_live_debris(scope, store):
    table, ours, other = scope
    alias = other / ".git" / store
    if store == "objects":
        (alias / "pack").rmdir()
    alias.rmdir()
    alias.symlink_to(ours / ".git" / store, target_is_directory=True)
    _process(table, 123, other)
    debris = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_live")
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert debris.exists()


@pytest.mark.parametrize("argv", [
    ("git", "index-pack", "--stdin", "{output}"),
    ("git-index-pack", "--stdin", "{output}"),
    ("git", "index-pack", "--stdin", "-o{output}"),
    ("git", "index-pack", "--stdin", "--", "-payload"),
    ("git-index-pack", "--stdin", "--", "-payload"),
])
def test_streamed_index_pack_can_still_name_an_explicit_output(scope, argv):
    table, ours, other = scope
    debris = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_live")
    argv = tuple(arg.replace("{output}", str(debris)) for arg in argv)
    name = "git-index-pack" if argv[0] != "git" else "git"
    _process(table, 123, other, name=name, argv=argv)
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert debris.exists()


@pytest.mark.parametrize("name,argv", [
    ("git", ("git", "index-pack", "--stdin", "--fix-thin", "--promisor")),
    ("git-index-pack", ("git-index-pack", "--stdin", "--fix-thin")),
    ("git", ("git", "pack-objects", "--stdout", "--revs", "--thin")),
])
def test_unrelated_repository_local_streams_do_not_veto_recovery(scope, name, argv):
    table, ours, other = scope
    _process(table, 123, other, name=name, argv=argv)
    debris = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    assert gitlock.clear_stale_tmp_packs(ours) == [str(debris)]


def test_empty_process_table_does_not_leave_dead_resources(scope):
    _, ours, _ = scope
    debris = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    assert gitlock.clear_stale_tmp_packs(ours) == [str(debris)]


def test_unreadable_git_ownership_keeps_resources(scope):
    table, ours, other = scope
    proc = _process(table, 123, other)
    (proc / "environ").unlink()
    lock = _aged(ours / ".git" / "index.lock")
    assert gitlock.clear_stale_git_locks(ours) == []
    assert lock.exists()


def test_symlink_loop_in_git_metadata_never_raises(scope):
    # Python 3.11's non-strict resolve() raises RuntimeError on a loop; the
    # sweep's contract is to veto or proceed, never to propagate.
    table, ours, _ = scope
    loop = ours / ".git" / "loop"
    loop.symlink_to(loop)
    (ours / ".git" / "commondir").write_text("loop\n")
    _process(table, 123, ours)
    lock = _aged(ours / ".git" / "index.lock")
    assert gitlock.clear_stale_git_locks(ours) == []
    assert lock.exists()


def test_linked_worktree_common_store_is_protected(scope, tmp_path):
    table, ours, _ = scope
    linked = tmp_path / "linked"
    linked.mkdir()
    metadata = ours / ".git" / "worktrees" / "linked"
    metadata.mkdir(parents=True)
    (metadata / "commondir").write_text("../..\n")
    (linked / ".git").write_text(f"gitdir: {metadata}\n")
    _process(table, 123, linked)
    debris = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert debris.exists()


def test_bare_repository_ownership(scope):
    table, ours, _ = scope
    _process(table, 123, ours / ".git", env=("GIT_DIR=.",))
    debris = _aged(ours / ".git" / "objects" / "pack" / "tmp_pack_dead")
    assert gitlock.clear_stale_tmp_packs(ours / ".git") == []
    assert debris.exists()


def test_fresh_temps_and_real_packs_survive_unrelated_git(scope):
    table, ours, other = scope
    _process(table, 123, other)
    pack = ours / ".git" / "objects" / "pack"
    fresh = pack / "tmp_pack_fresh"
    fresh.write_bytes(b"live")
    real = _aged(pack / "pack-real.pack")
    assert gitlock.clear_stale_tmp_packs(ours) == []
    assert fresh.exists() and real.exists()
