"""Generation ownership fences for startup-only session recovery."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def startup_recovery_fences(runner) -> tuple[frozenset[str], frozenset[str]]:
    """Return living-owner and unsafe-to-retry keys, not historical claim keys.

    Death permits final delivery, never replay of unfinished owned work. A clean
    release is different: an exited zero-work claim with no pending input or
    interruption evidence is historical, but only after death proof. A claim
    reassigned to a living successor remains protected; zero work alone cannot
    prove that the successor released it. Failed/unknown claims are ambiguous.
    """
    from gateway.generation import overlap_handover_enabled
    if not overlap_handover_enabled(getattr(runner, "config", None)):
        return frozenset(), frozenset()
    from gateway.owned_admission import OwnedAdmissionMixin
    from hermes_constants import get_process_hermes_home

    path = Path(get_process_hermes_home()) / "gateway-coordinator.db"
    if not path.exists():
        return frozenset(), frozenset()
    with runner.session_store._lock:
        runner.session_store._ensure_loaded_locked()
        scope = {
            (str(runner._resolve_profile_home_for_source(entry.origin)),
             entry.origin.platform.value, entry.session_key)
            for entry in runner.session_store._entries.values() if entry.origin is not None
        }
    # A single SELECT is a consistent read snapshot. Do not initialize/migrate
    # the coordinator or hold its writer lock while probing OS process identity.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        # N-1 stores failure in state. Migrated writers preserve it in verdict.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(generations)")}
        verdict = "g.verdict" if "verdict" in columns else "NULL"
        rows = db.execute(
            "SELECT s.*,g.id,g.pid,g.boot_id,g.start_fingerprint,g.state AS owner_state,"
            f"{verdict} AS owner_verdict,"
            "EXISTS (SELECT 1 FROM inbox i WHERE i.profile_home=s.profile_home "
            "AND i.transport=s.transport AND i.session_key=s.session_key "
            "AND i.owner_id=s.generation_id AND i.state='pending') AS pending "
            "FROM sessions s LEFT JOIN generations g ON g.id=s.generation_id"
        ).fetchall()
    live, unsafe, dead = set(), set(), {}
    for row in rows:
        if (row["profile_home"], row["transport"], row["session_key"]) not in scope:
            continue
        key, owner = row["session_key"], row["id"]
        cut = row["state"] == "interrupted" or row["outstanding_work"] > 0 or row["pending"]
        if owner not in dead:
            dead[owner] = owner is not None and OwnedAdmissionMixin._owner_is_dead(row)
        if not dead[owner]:
            live.add(key)
            unsafe.add(key)
        elif cut or row["owner_state"] != "exited" or row["owner_verdict"] == "failed":
            unsafe.add(key)
    # Startup intake has not begun; an excluded live claim may release after the
    # snapshot (conservatively deferred). A proven-dead identity cannot revive.
    return frozenset(live), frozenset(unsafe)
