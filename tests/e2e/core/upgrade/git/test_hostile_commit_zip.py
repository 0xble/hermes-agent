"""Hostile commit: journaled ZIP recovery.

See ``_hostile_commit`` for the shared harness and full cell contract.
Every cell keeps a fresh real install; no mutable checkout is shared.
"""
from __future__ import annotations

from tests.e2e.core.upgrade.git import _hostile_commit as C
from tests.e2e.core.upgrade.git._hostile_commit import (
    I,
    P,
    _artifacts,
    _head,
    _launch,
    _tracked_dirty,
    _update,
    _zip_killed_at,
    provider as provider,
    world as world,
)
from tests.e2e.core.upgrade.git._hostile_diagnostics import (
    hostile_cell as hostile_cell,
    hostile_diagnostics as hostile_diagnostics,
)

pytestmark = C.pytestmark


def test_kill_mid_zip_swap_is_settled_by_the_next_launch(world):
    sb = world["sb"]
    pre = _head(sb)
    # Killed right after a TRACKED file's new bytes landed: the swap order follows the archive's
    # directory listing, so a "kill at the N-th swap" can land on N unchanged entries only.
    agents = I.git("show", "HEAD:AGENTS.md", cwd=world["origin"]) + "\n<!-- e2e zip swap -->\n"
    killed = _zip_killed_at(world, "after:AGENTS.md", {"AGENTS.md": agents})
    at_kill = {"artifacts": _artifacts(sb), "dirty": _tracked_dirty(sb),
               "new_entry": (sb.checkout / "e2e_zip_release.py").exists()}
    assert at_kill["artifacts"] and at_kill["dirty"], (
        f"harness: the ZIP swap was not killed mid-rename ({at_kill['dirty']!r})\n" + I.describe(killed))
    launch = _launch(sb, "first launch after a kill mid ZIP swap")
    leftovers = _artifacts(sb)
    assert not leftovers, (f"{len(leftovers)} staging/backup siblings leaked and wedge the next update: "
                           f"{leftovers[:5]} (at kill: {len(at_kill['artifacts'])} siblings, "
                           f"tracked changes {at_kill['dirty']!r})\n" + I.describe(launch))
    assert _head(sb) == pre and not _tracked_dirty(sb), (
        "the interrupted ZIP swap left a mixed tree:\n" + _tracked_dirty(sb) + "\n" + I.describe(launch))


def test_kill_mid_zip_swap_with_hermes_cli_moved_aside_reaches_recovery(world):
    """The one window where the recovery code itself is gone: the swap renamed ``hermes_cli/`` aside
    and died before the replacement landed. The next launch must still reach the journal-driven
    restore (from the moved-aside copy) and run, not die on ``No module named 'hermes_cli'``."""
    sb = world["sb"]
    pre = _head(sb)
    killed = _zip_killed_at(world, "hermes_cli-moved-aside")
    assert not (sb.checkout / "hermes_cli").exists() and (sb.checkout / "hermes_cli.hermes-update-old").is_dir(), (
        "harness: the ZIP swap was not killed with hermes_cli/ moved aside\n" + I.describe(killed))
    launch = _launch(sb, "first launch after a kill with hermes_cli moved aside")
    assert launch.returncode == 0 and (sb.checkout / "hermes_cli" / "main.py").is_file(), (
        "the launch after a kill with hermes_cli/ moved aside could not reach recovery\n" + I.describe(launch))
    assert not _artifacts(sb) and _head(sb) == pre and not _tracked_dirty(sb), (
        f"left {_artifacts(sb)[:5]} / tracked changes {_tracked_dirty(sb)!r}\n" + I.describe(launch))
    P.ok(_update(sb), "the update after the restored swap failed")
