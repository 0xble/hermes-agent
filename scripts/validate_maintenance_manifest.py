#!/usr/bin/env python3
"""Assess the maintained-fork inventory without making it a delivery gate."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
import re
from typing import Iterable

_INDEX_ROW_RE = re.compile(r"^\|\s*(HERMES-\d+)\s*\|\s*(Active|Retired)\s*\|")
_RECORD_RE = re.compile(r"^###\s+(HERMES-\d+)\b")


def _duplicates(values: Iterable[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


def assess_manifest(path: Path) -> list[str]:
    """Return useful inventory findings; never inspect Git history or subjects."""
    if path.is_symlink() or not path.is_file():
        raise OSError("manifest must be a regular non-symlink file")
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        index_start = lines.index("## Maintained patch index")
        records_start = lines.index("## Patch records")
    except ValueError as exc:
        return [f"missing inventory section: {exc.args[0]}"]
    if records_start <= index_start:
        return ["Patch records section should follow maintained patch index"]
    rows = [_INDEX_ROW_RE.match(line) for line in lines[index_start:records_start]]
    row_ids = [match.group(1) for match in rows if match]
    records = [_RECORD_RE.match(line) for line in lines[records_start + 1 :]]
    record_ids = [match.group(1) for match in records if match]
    findings = [f"duplicate patch index ID: {patch_id}" for patch_id in _duplicates(row_ids)]
    findings += [f"duplicate patch record ID: {patch_id}" for patch_id in _duplicates(record_ids)]
    row_set, record_set = set(row_ids), set(record_ids)
    findings += [f"unindexed patch record: {patch_id}" for patch_id in sorted(record_set - row_set)]
    findings += [f"indexed patch has no record: {patch_id}" for patch_id in sorted(row_set - record_set)]
    return findings


def validate_manifest(path: Path, **_ignored: object) -> list[str]:
    """Compatibility name for callers; findings are advisory only."""
    return assess_manifest(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", type=Path, default=Path("MAINTENANCE.md"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        findings = assess_manifest(args.manifest)
        payload = {
            "status": "findings" if findings else "passed",
            "assessed": True, "findings": findings, "errors": [],
        }
    except (OSError, UnicodeError) as exc:
        findings = []
        payload = {"status": "error", "assessed": False, "findings": [],
                   "errors": [f"inventory not assessed: {exc}"]}
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    elif payload["errors"]:
        print("ADVISORY: inventory not assessed")
    elif findings:
        for finding in findings:
            print(f"ADVISORY: {finding}")
    else:
        print(f"maintenance inventory OK: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
