"""Read-only view of opt-in gateway generations."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def read_generation_status(home: Path) -> list[dict[str, Any]]:
    """Return recorded generations and their leases without creating coordinator state."""
    path = Path(home) / "gateway-coordinator.db"
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(
                "SELECT id,release_sha,label,pid,start_fingerprint,state,heartbeat_at "
                "FROM generations ORDER BY started_at,id")]
            leases: dict[str, list[str]] = {}
            for row in conn.execute("SELECT resource,epoch,generation_id,state FROM leases"):
                leases.setdefault(row["generation_id"], []).append(
                    f"{row['resource']}@{row['epoch']}:{row['state']}")
            for row in rows:
                row["leases"] = leases.get(row["id"], [])
            return rows
    except (sqlite3.Error, OSError):
        return []
