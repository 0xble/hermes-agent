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
_SUPPORT_LINK_RE = re.compile(r"\[[^\]\n]+\]\((maintenance/[^)\n]+)\)")
_SUPPORT_PATH_RE = re.compile(r"maintenance/[a-z][a-z0-9-]*\.md")
_REQUIRED_RE = re.compile(r"\bRequired (?:every run|when [^:*\n]+):", re.IGNORECASE)



def _duplicates(values: Iterable[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


def _read_lines(path: Path) -> list[str]:
    if path.is_symlink() or not path.is_file():
        raise OSError(f"contract must be a regular non-symlink file: {path}")
    return path.read_text(encoding="utf-8").splitlines()


def _contract_pages(path: Path) -> tuple[list[tuple[Path, list[str]]], list[str]]:
    """Read root-declared flat modules, never silently pass a partial closure."""
    lines = _read_lines(path)
    pages = [(path, lines)]
    findings: list[str] = []
    declared: list[str] = []
    for line in lines:
        for match in _SUPPORT_LINK_RE.finditer(line):
            target = match.group(1)
            if not _SUPPORT_PATH_RE.fullmatch(target):
                raise OSError(f"support link must be a flat maintenance/topic.md path: {target}")
            declared.append(target)
            if not _REQUIRED_RE.search(line[:match.start()]):
                findings.append(f"support link has no required every-run or concrete trigger: {target}")
    support = path.parent / "maintenance"
    if support.is_symlink():
        raise OSError(f"support directory must not be a symlink: {support}")
    if declared or support.exists():
        if lines.count("## Background") != 1:
            findings.append("root contract must contain exactly one ## Background")
        if support.exists() and not support.is_dir():
            raise OSError(f"support path must be a directory: {support}")
        if support.is_dir():
            for entry in sorted(support.iterdir()):
                relative = f"maintenance/{entry.name}"
                if entry.is_symlink():
                    raise OSError(f"support entry must not be a symlink: {relative}")
                if entry.is_dir():
                    findings.append(f"support modules must be flat: {relative}")
                elif entry.suffix.lower() == ".md":
                    if entry.stem.lower() in {"maintenance", "index", "readme", "manifest"}:
                        findings.append(f"support entry is not a responsibility module: {relative}")
                    if relative not in declared:
                        findings.append(f"unlinked support module: {relative}")
    findings += [f"duplicate support declaration: {target}" for target in _duplicates(declared)]
    for target in dict.fromkeys(declared):
        module = path.parent / target
        pages.append((module, _read_lines(module)))
    return pages, findings


def assess_manifest(path: Path) -> list[str]:
    """Assess the complete contract inventory, never Git history or subjects.

    Legacy inline inventories remain supported. A modular root may omit its own
    index, but every declared module must carry the paired inventory sections.
    Both findings and an unassessable closure remain non-publication-blocking.
    """
    pages, findings = _contract_pages(path)
    row_ids: list[str] = []
    record_ids: list[str] = []
    for page, lines in pages:
        if page == path and len(pages) > 1 and not any(
            line in {"## Maintained patch index", "## Patch records"}
            or _INDEX_ROW_RE.match(line) or _RECORD_RE.match(line) for line in lines
        ):
            continue
        try:
            index_start = lines.index("## Maintained patch index")
            records_start = lines.index("## Patch records")
        except ValueError as exc:
            findings.append(f"missing inventory section in {page.name}: {exc.args[0]}")
            continue
        if records_start <= index_start:
            findings.append(f"Patch records section should follow maintained patch index: {page.name}")
            continue
        rows = [_INDEX_ROW_RE.match(line) for line in lines[index_start:records_start]]
        row_ids.extend(match.group(1) for match in rows if match)
        records = [_RECORD_RE.match(line) for line in lines[records_start + 1 :]]
        record_ids.extend(match.group(1) for match in records if match)
    findings += [f"duplicate patch index ID: {patch_id}" for patch_id in _duplicates(row_ids)]
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
