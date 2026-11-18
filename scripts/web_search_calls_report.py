#!/usr/bin/env python3
"""Summarize $HERMES_HOME/logs/web_search_calls.jsonl by day, surface, root session and cache outcome.

Read-only. Usage: python scripts/web_search_calls_report.py [PATH]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_constants import get_hermes_home  # noqa: E402
from tools.web_search_attribution import summarize_records  # noqa: E402


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else get_hermes_home() / "logs" / "web_search_calls.jsonl"
    if not path.is_file():
        print(f"no such log: {path}", file=sys.stderr)
        return 1
    for row in summarize_records(path):
        print(json.dumps(row, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
