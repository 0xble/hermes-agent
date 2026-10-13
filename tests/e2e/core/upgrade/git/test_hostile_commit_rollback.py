"""Hostile commit: killed git reset recovery.

See ``_hostile_commit`` for the shared harness and full cell contract.
Every cell keeps a fresh real install; no mutable checkout is shared.
"""
from __future__ import annotations

from tests.e2e.core.upgrade.git import _hostile_commit as C
from tests.e2e.core.upgrade.git._hostile_commit import (
    I,
    P,
    _GIT_KILLER,
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


def test_git_killed_inside_the_rollback_reset_is_finished_by_the_next_launch(world):
    sb = world["sb"]
    pre = _head(sb)
    # A critical module the launcher imports only after the launch-time repair ran (the launcher's own
    # first import, hermes_constants, is a documented limit: a broken copy dies before any repair).
    tools = I.git("show", "main:model_tools.py", cwd=world["origin"])
    broken = _release(world, {"model_tools.py": tools + "\ndef broken(:\n",
                              "e2e_rollback_reset_extra.py": "X = 1\n"})
    killer = world["root"] / "git_killer.py"
    killer.write_text(_GIT_KILLER, encoding="utf-8")
    once = sb.root / "rollback-reset-killed"  # the sandbox can write only under its own root
    # The preflight reads the old file (so the broken release passes it); the rollback's first step,
    # `reset -q <pre>` (HEAD and index, no file), runs once under the killer.
    _hostile_git(world, 'case " $* " in *" show "*":model_tools.py "*) "$REAL" show HEAD:model_tools.py; '
                        'exit $?;; '
                        '*" cat-file --batch "*) sed "s#^[^ ]*:model_tools.py\\$#HEAD:model_tools.py#" | "$REAL" "$@"; exit $?;; '
                        f'*" reset -q {pre} "*) if [ ! -e "{once}" ]; then : > "{once}"; '
                        f'exec "{sb.python}" "{killer}" "$REAL" "$@"; fi;; esac')
    killed = _update(sb)
    _hostile_git(world, "")
    output = (killed.stdout or "") + (killed.stderr or "")
    lock = sb.checkout / ".git" / "index.lock"
    assert "syntax error" in output and once.exists(), "harness: the rollback reset never ran\n" + I.describe(killed)
    assert lock.exists() and _head(sb) == broken, (
        "harness: the kill did not land while git held index.lock\n" + I.describe(killed))
    assert killed.returncode != 0 and "Rollback complete" not in output, (
        "the updater reported a rollback that never happened\n" + I.describe(killed))
    assert (sb.checkout / ".git" / "hermes-update-pull").is_file(), (
        "the killed rollback erased its own recovery record while HEAD is still the broken release\n"
        + I.describe(killed))

    launch = _launch(sb, "first launch after git was killed inside the rollback")
    assert _head(sb) == pre and not _tracked_dirty(sb) and not lock.exists(), (
        f"the launch did not finish the rollback to {pre[:10]} (HEAD {_head(sb)[:10]}, broken {broken[:10]}, "
        f"lock {lock.exists()}, tracked changes {_tracked_dirty(sb)!r})\n" + P.diagnostics(sb, killed, launch))
    assert not (sb.checkout / "e2e_rollback_reset_extra.py").exists()
    assert not (sb.checkout / ".git" / "hermes-update-pull").exists()
