"""Hostile commit: ZIP recovery over a leftover backup.

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
    _release,
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


def test_kill_mid_zip_swap_over_leftover_backup_keeps_the_entry(world):
    """A pre-journal crash left ``e2e_zip_lost.hermes-update-old`` as the ONLY copy of a tracked dir.
    The next swap heals it, swaps it, and is killed right after its replacement landed: recovery
    must put the old dir back, never drop both copies."""
    sb = world["sb"]
    pre = _release(world, {"e2e_zip_lost/m.py": "OLD = 1\n"})
    P.ok(_update(sb), "harness: plain update failed")
    lost = sb.checkout / "e2e_zip_lost"
    lost.rename(sb.checkout / "e2e_zip_lost.hermes-update-old")
    killed = _zip_killed_at(world, "after:e2e_zip_lost", {"e2e_zip_lost/m.py": "NEW = 1\n"})
    assert (lost / "m.py").read_text(encoding="utf-8-sig") == "NEW = 1\n" and (sb.checkout / "e2e_zip_lost.hermes-update-old").is_dir(), (
        "harness: the swap was not killed right after e2e_zip_lost landed\n" + I.describe(killed))
    launch = _launch(sb, "first launch after a kill over a leftover backup")
    assert (lost / "m.py").is_file() and (lost / "m.py").read_text(encoding="utf-8-sig") == "OLD = 1\n", (
        "recovery deleted both copies of an entry whose backup was healed before the swap: "
        f"{sorted(p.name for p in sb.checkout.iterdir() if p.name.startswith('e2e_zip'))}\n" + I.describe(launch))
    assert not _artifacts(sb) and _head(sb) == pre and not _tracked_dirty(sb), (
        f"left {_artifacts(sb)[:5]} / tracked changes {_tracked_dirty(sb)!r}\n" + I.describe(launch))
