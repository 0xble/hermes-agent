"""Historical N-1 autostash upgrade behavior with a fresh isolated install."""
from __future__ import annotations

from tests.e2e.core.upgrade import _helpers as H
from tests.e2e.core.upgrade import test_upgrade_path as U
from tests.e2e.core.upgrade.test_upgrade_path import (
    _git,
    _publish_head,
    _refs,
    _update,
    _upgrade_prerequisites,
    assert_healthy_at_head,
    install_cache,
    leg,
    provider,
    template_home,
)

pytestmark = U.pytestmark


def test_update_with_local_edits_and_orphan_autostash(leg, provider):
    # A file that does not change N-1 -> HEAD, so restoring the user's edit is conflict-free.
    unchanged = next(p for p in ("README.md", "LICENSE", "AGENTS.md")
                     if (leg.install / p).exists()
                     and not _git("diff", "--name-only", _refs().base, _refs().head, "--", p, cwd=H.WORKTREE))
    # An orphan autostash from an earlier update that never restored it (#63717).
    (leg.install / unchanged).write_text((leg.install / unchanged).read_text() + "\norphan edit\n")
    _git("stash", "push", "-m", "hermes-update-autostash-20260101-000000", cwd=leg.install)
    orphan = _git("rev-parse", "refs/stash", cwd=leg.install)
    # The user's current local work: a tracked edit and an untracked file.
    user_line = "\nuser's local note that must survive the update\n"
    (leg.install / unchanged).write_text((leg.install / unchanged).read_text() + user_line)
    (leg.install / "my_notes.txt").write_text("untracked user file\n")
    _publish_head(leg)
    final = _update(leg)
    assert_healthy_at_head(leg, provider, final)
    stash_commits = _git("log", "-g", "--format=%H", "refs/stash", cwd=leg.install, check=False).split()
    assert orphan in stash_commits, "the pre-existing autostash (maybe the only copy of user work) was dropped"
    # The user's work is either restored in the tree or parked in a stash entry: never lost.
    restored = user_line.strip() in (leg.install / unchanged).read_text()
    parked = any(user_line.strip() in _git("stash", "show", "-p", "--include-untracked", c, cwd=leg.install, check=False)
                 for c in stash_commits if c != orphan)
    assert restored or parked, "local tracked edit lost by the update"
    untracked_ok = (leg.install / "my_notes.txt").exists() or any(
        "my_notes.txt" in _git("show", "--name-only", "--format=", f"{c}^3", cwd=leg.install, check=False)
        for c in stash_commits if c != orphan)
    assert untracked_ok, "untracked user file lost by the update"

