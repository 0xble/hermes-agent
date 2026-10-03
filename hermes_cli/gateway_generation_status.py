"""Read-only view of opt-in gateway generations."""
from __future__ import annotations

import json
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
                "SELECT * "
                "FROM generations ORDER BY started_at DESC,id DESC").fetchall()
            rows = [dict(record) for record in recorded]
            rows.reverse()
            leases: dict[str, list[str]] = {}
            for row in conn.execute("SELECT resource,epoch,generation_id,state FROM leases WHERE state!='released'"):
                leases.setdefault(row["generation_id"], []).append(
                    f"{row['resource']}@{row['epoch']}:{row['state']}")
            for row in rows:
                row["leases"] = leases.get(row["id"], [])
                state_file = Path(home) / f"gateway_state.{row['id']}.json"
                try:
                    health = json.loads(state_file.read_text(encoding="utf-8-sig"))
                    if health.get("id") == row["id"] and health.get("start_fingerprint") == row["start_fingerprint"]:
                        row["needs_attention"] = health.get("needs_attention", False)
                        row["polling"] = health.get("polling")
                except (OSError, ValueError, TypeError):
                    pass
            return rows
    except (sqlite3.Error, OSError):
        return []
