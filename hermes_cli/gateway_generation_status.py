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
            recorded = conn.execute(
                "SELECT id,release_sha,label,pid,start_fingerprint,state,heartbeat_at,drain_deadline "
                "FROM generations ORDER BY started_at DESC,id DESC").fetchall()
            terminal_labels: set[str] = set()
            rows = []
            for record in recorded:
                row = dict(record)
                if row["state"] in {"exited", "failed"}:
                    if row["label"] in terminal_labels:
                        continue
                    terminal_labels.add(row["label"])
                rows.append(row)
            rows.reverse()
            leases: dict[str, list[str]] = {}
            for row in conn.execute("SELECT resource,epoch,generation_id,state FROM leases WHERE state!='released'"):
                leases.setdefault(row["generation_id"], []).append(
                    f"{row['resource']}@{row['epoch']}:{row['state']}")
            polling = conn.execute("SELECT generation_id,epoch FROM leases "
                                   "WHERE resource='active_generation' AND state='active'").fetchone()
            outstanding = {row["generation_id"]: row["n"] for row in conn.execute(
                "SELECT generation_id,SUM(outstanding_work) AS n FROM sessions GROUP BY generation_id")}
            for row in rows:
                row["leases"] = leases.get(row["id"], [])
                row["polling_owner"] = bool(polling and polling["generation_id"] == row["id"])
                row["draining_count"] = outstanding.get(row["id"], 0) if row["state"] == "draining" else 0
            return rows
    except (sqlite3.Error, OSError):
        return []
