"""Hostile commit: tree commit and torn-pull recovery.

See ``_hostile_commit`` for the shared harness and full cell contract.
Every cell keeps a fresh real install; no mutable checkout is shared.
"""
from __future__ import annotations

from tests.e2e.core.upgrade.git import _hostile_commit as C
from tests.e2e.core.upgrade.git._hostile_commit import (
    I,
    P,
    _head,
    _hostile_git,
    _launch,
    _release,
    _stamp_commit,
    _tracked_dirty,
    _update,
    provider as provider,
    world as world,
)
from tests.e2e.core.upgrade.git._hostile_diagnostics import (
    hostile_cell as hostile_cell,
    hostile_diagnostics as hostile_diagnostics,
)

pytestmark = C.pytestmark


def test_kill_after_tree_moved_still_owes_the_tail(world):
    sb = world["sb"]
    pre = _head(sb)
    assert _stamp_commit(sb) == pre and not P.pending_marker(sb).exists(), "harness: install not settled"
    target = _release(world, {"e2e_hostile_release.py": "RELEASE = 1\n"})
    # git completes the fast-forward, then the updater dies before it can spawn the completion child.
    _hostile_git(world, 'case " $* " in *" merge --ff-only "*) "$REAL" "$@"; rc=$?; kill -KILL $PPID; exit $rc;; esac')
    killed = _update(sb)
    _hostile_git(world, "")
    assert _head(sb) == target, "harness: the kill did not land after the tree moved\n" + I.describe(killed)
    owed = P.pending_marker(sb).exists()

    launch = _launch(sb, "first launch after a kill past the swap")
    assert owed, ("the tree moved to the release but no source-completion tail was owed: the kill "
                  "before the completion child lost it\n" + P.diagnostics(sb, killed, launch))
    assert not P.pending_marker(sb).exists() and _stamp_commit(sb) == target, (
        "the next launch did not finish the owed tail for the moved tree\n" + P.diagnostics(sb, killed, launch))


def test_torn_tree_on_git_failure_is_restored(world):
    sb = world["sb"]
    # Release 1 adds a file under a directory; the user makes that directory read-only.
    ok = _release(world, {"aaa_e2e_first.py": "V = 1\n", "zzz_e2e_locked/mod.py": "V = 1\n"})
    P.ok(_update(sb), "harness: plain update to the first release failed")
    assert _head(sb) == ok
    _release(world, {"aaa_e2e_first.py": "V = 2\n", "zzz_e2e_locked/mod.py": "V = 2\n"})
    locked = sb.checkout / "zzz_e2e_locked"
    locked.chmod(0o555)
    try:
        failed = _update(sb)
    finally:
        locked.chmod(0o755)
    assert failed.returncode != 0, "harness: git did not fail on the read-only directory\n" + I.describe(failed)
    launch = _launch(sb, "first launch after a torn pull")
    assert _head(sb) == ok and not _tracked_dirty(sb), (
        "git failed mid-pull and the checkout was left torn (marker dropped, nothing restores it):\n"
        + _tracked_dirty(sb) + "\n" + P.diagnostics(sb, failed, launch))
