#!/usr/bin/env python3
"""Read-only ownership-goal hygiene report.

Usage: python scripts/goal_hygiene.py ~/.hermes/state.db --json
The URI uses mode=ro and immutable=0; this command never writes or copies state.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes_cli.goal_lints import _approval_without_safety

_CACHE = Path(os.path.abspath(os.path.expanduser("~/.hermes/cache")))


def _under_cache(value: Any) -> bool:
    if not str(value or "").strip():
        return False
    try:
        Path(str(value)).expanduser().absolute().relative_to(_CACHE)
        return True
    except (ValueError, OSError):
        return False


def _path_missing(value: Any) -> bool:
    return bool(str(value or "").strip()) and not Path(str(value)).expanduser().exists()


def report(db_path: str) -> dict[str, int]:
    uri = f"file:{Path(db_path).expanduser().resolve()}?mode=ro&immutable=0"
    db = sqlite3.connect(uri, uri=True)
    try:
        rows = db.execute("SELECT value FROM state_meta WHERE key LIKE 'goal:%'").fetchall()
    finally:
        db.close()
    goals: list[dict[str, Any]] = []
    for (raw,) in rows:
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            goals.append(item)
    active = [g for g in goals if g.get("status") == "active"]
    owned = [g for g in goals if g.get("ownership") or any(g.get(k) for k in ("due", "done_when", "log_path", "plan_path"))]
    missing_acceptance = [g for g in active if not str(g.get("due") or "").strip() or not str(g.get("done_when") or g.get("verification") or "").strip()]
    bad_paths = [g for g in goals if any(_under_cache(g.get(k)) or _path_missing(g.get(k)) for k in ("plan_path", "log_path"))]
    approval = [g for g in goals if any(_approval_without_safety(str((g.get("contract") or {}).get(k) or g.get(k) or "")) for k in ("constraints", "stop_when")) and not g.get("user_quote")]
    wait_without_deadline = [g for g in goals if (g.get("last_verdict") == "wait" or g.get("waiting_reason")) and not float(g.get("waiting_until") or 0)]
    return {
        "active_goals": len(active),
        "goals_missing_due_or_done_when": len(missing_acceptance),
        "plan_or_log_paths_under_cache_or_missing": len(bad_paths),
        "approval_wording_without_quote_or_safety_class": len(approval),
        "wait_without_waiting_until": len(wait_without_deadline),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("state_db")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    result = report(args.state_db)
    print(json.dumps(result, sort_keys=True) if args.as_json else "\n".join(f"{k}: {v}" for k, v in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
