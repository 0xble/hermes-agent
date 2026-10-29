"""Hostile commit: marked branch-switch recovery.

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
    _update,
    provider as provider,
    world as world,
)
from tests.e2e.core.upgrade.git._hostile_diagnostics import (
    hostile_cell as hostile_cell,
    hostile_diagnostics as hostile_diagnostics,
)

pytestmark = C.pytestmark


def test_kill_during_branch_switch_is_restored(world):
    sb = world["sb"]
    base = _head(sb)
    _release(world, {"e2e_cp0_a.py": "A = 1\n", "e2e_cp0_b.py": "B = 1\n"})
    P.ok(_update(sb), "harness: plain update failed")
    # Park the checkout on a fully merged branch one release behind main.
    I.git("checkout", "-q", "-b", "e2e-parked", base, cwd=sb.checkout)
    parked = _head(sb)
    _release(world, {"e2e_cp0_c.py": "C = 1\n"})
    # CP0: `git checkout main` dies after rewriting one file of main's tree.
    _hostile_git(world, 'if [ "${@: -1}" = main ] && case " $* " in *" checkout "*) true;; *) false;; esac; then '
                        '"$REAL" show main:e2e_cp0_a.py > e2e_cp0_a.py; kill -KILL $PPID; exit 137; fi')
    killed = _update(sb)
    _hostile_git(world, "")
    assert (sb.checkout / "e2e_cp0_a.py").is_file() and _head(sb) == parked, (
        "harness: the kill did not land mid-switch\n" + I.describe(killed))
    launch = _launch(sb, "first launch after a kill during the branch switch")
    assert _head(sb) == parked and not (sb.checkout / "e2e_cp0_a.py").exists(), (
        "a kill during the CP0 branch switch left main's files in the parked checkout\n"
        + P.diagnostics(sb, killed, launch))


def test_kill_during_branch_switch_without_local_branch_is_restored(world):
    """The parked checkout has no local ``main``: only ``origin/main``. Whichever checkout actually
    moves the tree to main must run under the marker."""
    sb = world["sb"]
    base = _head(sb)
    _release(world, {"e2e_dwim_a.py": "A = 1\n"})
    P.ok(_update(sb), "harness: plain update failed")
    I.git("checkout", "-q", "-b", "e2e-parked", base, cwd=sb.checkout)
    I.git("branch", "-q", "-D", "main", cwd=sb.checkout)
    parked = _head(sb)
    _release(world, {"e2e_dwim_c.py": "C = 1\n"})
    # Die after writing one of main's files, in the checkout that moves the tree: a local main
    # exists, `-B` was given, or git may guess (no --no-guess) and create main from origin/main.
    # `-B main` names the resolved commit, not the ref (review O2), so it moves whatever comes last.
    _hostile_git(world, 'case " $* " in *" checkout "*) moves=0; case " $* " in *" -B main "*) moves=1;; esac; '
                        'case "${@: -1}" in main|origin/main) '
                        '"$REAL" rev-parse -q --verify refs/heads/main >/dev/null && moves=1; '
                        'case " $* " in *" --no-guess "*) ;; *) moves=1;; esac;; esac; '
                        'if [ $moves = 1 ]; then "$REAL" show origin/main:e2e_dwim_a.py > e2e_dwim_a.py; '
                        'kill -KILL $PPID; exit 137; fi;; esac')
    killed = _update(sb)
    _hostile_git(world, "")
    assert (sb.checkout / "e2e_dwim_a.py").is_file() and _head(sb) == parked, (
        "harness: the kill did not land mid-switch\n" + I.describe(killed))
    launch = _launch(sb, "first launch after a kill during a guessed branch switch")
    assert _head(sb) == parked and not (sb.checkout / "e2e_dwim_a.py").exists(), (
        "a kill while git created main from origin/main left main's files in the parked checkout "
        "(no interrupted-pull marker)\n" + P.diagnostics(sb, killed, launch))
