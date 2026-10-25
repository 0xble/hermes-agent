"""Hostile commit: failed fork fast-forward recovery.

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


def test_failed_upstream_sync_after_origin_pull_keeps_the_tail_owed(world):
    """Fork checkout: the origin pull moves the tree (committed), then the upstream fast-forward fails
    on a read-only directory and is put back to the post-origin commit. That failure must not hand
    back the obligations the origin pull armed: a kill right after it still owes the tail."""
    sb, root = world["sb"], world["root"]
    first = _release(world, {"zzz_e2e_fork/mod.py": "V = 1\n"})
    P.ok(_update(sb), "harness: plain update to the first release failed")
    assert _head(sb) == first and _stamp_commit(sb) == first
    moved = _release(world, {"e2e_fork_origin.py": "B = 1\n"})
    upstream = root / "upstream.git"
    I.git("clone", "-q", "--bare", "--shared", str(world["origin"]), str(upstream), cwd=root)
    up = I.publish_commit(upstream, root, "upstream: touches the locked dir", {"zzz_e2e_fork/mod.py": "V = 2\n"})
    I.git("remote", "add", "upstream", str(upstream), cwd=sb.checkout)
    flag = sb.checkout / ".git" / "e2e-upstream-ff-failed"
    # origin reads as a fork (the sandbox shim otherwise reports the official URL). Kill the updater at
    # its next branch check once the upstream merge has failed (and been settled).
    _hostile_git(world, 'case " $* " in *" remote get-url origin "*) echo https://github.com/e2e-fork/hermes-agent.git; '
                        'exit 0;; esac\n'
                        # The sync fast-forwards to the upstream commit it resolved (one SHA, m2).
                        f'case " $* " in *" merge --ff-only "*"{up} "*) "$REAL" "$@"; rc=$?; '
                        f'[ $rc != 0 ] && touch "{flag}"; exit $rc;; esac\n'
                        f'if [ -e "{flag}" ]; then case " $* " in *" rev-parse --abbrev-ref HEAD "*) '
                        'kill -KILL $PPID; exit 137;; esac; fi')
    locked = sb.checkout / "zzz_e2e_fork"
    locked.chmod(0o555)
    try:
        killed = _update(sb)
    finally:
        locked.chmod(0o755)
        _hostile_git(world, "")
    assert flag.exists() and killed.returncode != 0 and _head(sb) == moved and not _tracked_dirty(sb), (
        "harness: the upstream ff did not fail after the origin pull moved the tree\n" + I.describe(killed))
    owed = P.pending_marker(sb).exists()
    launch = _launch(sb, "first launch after a failed upstream sync")
    assert owed, ("the origin pull moved the tree but the failed upstream ff disarmed its owed tail\n"
                  + P.diagnostics(sb, killed, launch))
    assert _stamp_commit(sb) == moved and not P.pending_marker(sb).exists(), (
        "the next launch did not finish the tail for the moved tree\n" + P.diagnostics(sb, killed, launch))
