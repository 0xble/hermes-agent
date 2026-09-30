"""Generation ownership fences for startup-only session recovery."""
from __future__ import annotations

from pathlib import Path


def startup_recovery_fences(runner) -> tuple[frozenset[str], frozenset[str]]:
    """Return live-owner keys and all owned keys under the coordinator write fence.

    A missing clean-exit marker is normal while A drains. Only PID/boot/start
    identity death proof permits B to ledger A's final. Even after verified
    death, owned unfinished work is cut, not eligible for autonomous replay.
    Unowned legacy turns retain their existing crash-recovery policy.
    """
    if not getattr(getattr(runner, "config", None), "overlap_handover_enabled", False):
        return frozenset(), frozenset()
    from gateway.generation import GenerationCoordinator
    from hermes_constants import get_process_hermes_home

    home = Path(get_process_hermes_home())
    if not (home / "gateway-coordinator.db").exists():
        return frozenset(), frozenset()
    coordinator = GenerationCoordinator(home)
    live, owned = set(), set()
    with coordinator._transaction() as db, db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT s.session_key,g.id,g.pid,g.boot_id,g.start_fingerprint "
            "FROM sessions s LEFT JOIN generations g ON g.id=s.generation_id"
        ).fetchall()
        dead = {}
        for row in rows:
            key = row["session_key"]
            owned.add(key)
            owner = row["id"]
            if owner not in dead:
                # Neither a terminal state nor a stale heartbeat proves death.
                dead[owner] = owner is not None and coordinator._owner_is_dead(row)
            if not dead[owner]:
                live.add(key)
    return frozenset(live), frozenset(owned)
