"""Hostile commit: syntax refusal and killed rollback.

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


def test_syntax_error_target_is_refused_before_head_moves(world):
    sb = world["sb"]
    pre = _head(sb)
    constants = I.git("show", "main:hermes_constants.py", cwd=world["origin"])
    _release(world, {"hermes_constants.py": constants + "\ndef broken(:\n"})
    reflog_before = I.git("reflog", "-n", "20", "--format=%H %gs", cwd=sb.checkout)
    refused = _update(sb)
    reflog_after = I.git("reflog", "-n", "20", "--format=%H %gs", cwd=sb.checkout)
    assert refused.returncode != 0 and _head(sb) == pre, I.describe(refused)
    assert reflog_after == reflog_before, (
        "HEAD moved onto the uncompilable release before the syntax guard refused it:\n"
        + reflog_after.replace(reflog_before, "").strip() + "\n" + I.describe(refused))


def test_kill_during_syntax_rollback_lands_on_the_pre_update_commit(world):
    """A broken release the preflight cannot see (its target read is answered with the old file)
    is rolled back by the post-pull guard; the updater dies while ``reset --hard`` is writing the old
    files back. The next launch must finish on the pre-update commit, not restore the broken one."""
    sb = world["sb"]
    pre = _head(sb)
    constants = I.git("show", "main:hermes_constants.py", cwd=world["origin"])
    broken = _release(world, {"hermes_constants.py": constants + "\ndef broken(:\n",
                              "e2e_rollback_extra.py": "X = 1\n"})
    _hostile_git(world, 'case " $* " in *" show "*":hermes_constants.py "*) "$REAL" show HEAD:hermes_constants.py; '
                        'exit $?;; '
                        '*" cat-file --batch "*) sed "s#^[^ ]*:hermes_constants.py\\$#HEAD:hermes_constants.py#" | "$REAL" "$@"; exit $?;; '
                        '*" reset --hard "*) "$REAL" show "${@: -1}:hermes_constants.py" > hermes_constants.py; '
                        'kill -KILL $PPID; exit 137;; esac')
    killed = _update(sb)
    _hostile_git(world, "")
    assert "syntax error" in (killed.stdout or "") + (killed.stderr or ""), (
        "harness: the post-pull guard did not fire\n" + I.describe(killed))
    assert (sb.checkout / "e2e_rollback_extra.py").exists(), "harness: the kill did not land mid-rollback\n" + I.describe(killed)
    launch = _launch(sb, "first launch after a kill during the syntax rollback")
    assert _head(sb) == pre and not _tracked_dirty(sb) and not (sb.checkout / "e2e_rollback_extra.py").exists(), (
        f"the launch after a kill mid-rollback did not land on {pre[:10]} (HEAD {_head(sb)[:10]}, broken "
        f"{broken[:10]}; tracked changes {_tracked_dirty(sb)!r})\n" + P.diagnostics(sb, killed, launch))
