#!/usr/bin/env python3
"""Validate the maintained-fork patch index and lifecycle records."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import re
import subprocess
from typing import Iterable

_INDEX_ROW_RE = re.compile(
    r"^\|\s*(HERMES-\d+)\s*\|\s*(Active|Retired)\s*\|\s*(.*?)\s*\|"
)
_RECORD_RE = re.compile(r"^###\s+(HERMES-\d+)\b")
_SUBJECT_RE = re.compile(r"`([^`]+)`")


def _duplicates(values: Iterable[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


def _fork_subjects(repo: Path, upstream_ref: str) -> set[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s", f"{upstream_ref}..HEAD"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return {line for line in result.stdout.splitlines() if line}


def _registered_subjects(text: str) -> set[str]:
    """Return patch-index and administrative subjects from one manifest tree."""
    lines = text.splitlines()
    try:
        index_start = lines.index("## Maintained patch index")
        records_start = lines.index("## Patch records")
    except ValueError:
        return set()
    subjects: set[str] = set()
    for line in lines[index_start:records_start]:
        match = _INDEX_ROW_RE.match(line)
        if match:
            subjects.update(_SUBJECT_RE.findall(match.group(3)))
    exemption_heading = "## Fork-only administrative subject exemptions"
    if exemption_heading in lines:
        exemption_start = lines.index(exemption_heading) + 1
        for line in lines[exemption_start:records_start]:
            if line.startswith("## ") or line.startswith("### "):
                break
            if line.startswith("|") and not line.startswith("| ---"):
                subjects.update(_SUBJECT_RE.findall(line))
    return subjects


def _validate_registration_history(repo: Path, baseline: str) -> list[str]:
    """Reject fork commits registered only by a later descendant."""
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", baseline, "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        commits = subprocess.run(
            ["git", "rev-list", "--reverse", f"{baseline}..HEAD"],
            cwd=repo,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
    except subprocess.CalledProcessError:
        return [
            "maintenance history baseline is not an ancestor of HEAD: " + baseline
        ]

    errors: list[str] = []
    for commit in commits:
        subject = subprocess.run(
            ["git", "show", "-s", "--format=%s", commit],
            cwd=repo,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        manifest = subprocess.run(
            ["git", "show", f"{commit}:MAINTENANCE.md"],
            cwd=repo,
            check=False,
            text=True,
            capture_output=True,
        )
        if manifest.returncode != 0 or subject not in _registered_subjects(manifest.stdout):
            errors.append(
                f"fork subject was not registered in its own commit {commit[:12]}: {subject}"
            )
    return errors


def validate_manifest(
    path: Path,
    *,
    upstream_ref: str | None = None,
    fork_subjects: set[str] | None = None,
    history_baseline: str | None = None,
) -> list[str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    errors: list[str] = []

    try:
        index_start = lines.index("## Maintained patch index")
        records_start = lines.index("## Patch records")
    except ValueError as exc:
        return [f"missing required manifest section: {exc.args[0]}"]
    if records_start <= index_start:
        return ["Patch records section must follow maintained patch index"]

    rows = []
    for line in lines[index_start:records_start]:
        match = _INDEX_ROW_RE.match(line)
        if match:
            patch_id, status, subject_cell = match.groups()
            rows.append((patch_id, status, _SUBJECT_RE.findall(subject_cell)))

    row_ids = [row[0] for row in rows]
    for patch_id in _duplicates(row_ids):
        errors.append(f"duplicate patch index ID: {patch_id}")

    record_matches = [
        _RECORD_RE.match(line) for line in lines[records_start + 1 :]
    ]
    record_ids = [match.group(1) for match in record_matches if match]
    for patch_id in _duplicates(record_ids):
        errors.append(f"duplicate patch record ID: {patch_id}")

    row_set = set(row_ids)
    record_set = set(record_ids)
    for patch_id in sorted(record_set - row_set):
        errors.append(f"unindexed patch record: {patch_id}")
    for patch_id in sorted(row_set - record_set):
        errors.append(f"indexed patch has no record: {patch_id}")

    record_sections: dict[str, str] = {}
    current_id: str | None = None
    current_lines: list[str] = []
    for line in lines[records_start + 1 :]:
        match = _RECORD_RE.match(line)
        if match:
            if current_id is not None and current_id not in record_sections:
                record_sections[current_id] = "\n".join(current_lines)
            current_id = match.group(1)
            current_lines = [line]
        elif current_id is not None:
            current_lines.append(line)
    if current_id is not None and current_id not in record_sections:
        record_sections[current_id] = "\n".join(current_lines)

    for patch_id in sorted(record_set):
        section = record_sections.get(patch_id, "")
        if "- **Upstream tracking:**" not in section:
            errors.append(f"{patch_id} record missing Upstream tracking field")
        if "- **Upstream PR:**" not in section:
            errors.append(f"{patch_id} record missing Upstream PR field")

    for patch_id, status, subjects in rows:
        if not subjects:
            errors.append(f"{patch_id} index row has no stable commit subject")
        if status == "Active" and not subjects:
            errors.append(f"active patch {patch_id} has no stable commit subject")

    exemptions: dict[str, str] = {}
    exemption_heading = "## Fork-only administrative subject exemptions"
    if exemption_heading in lines:
        exemption_start = lines.index(exemption_heading) + 1
        for line in lines[exemption_start:]:
            if line.startswith("## ") or line.startswith("### "):
                break
            if not line.startswith("|") or line.startswith("| ---"):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            subjects = _SUBJECT_RE.findall(cells[0]) if cells else []
            if not subjects:
                continue
            subject = subjects[0]
            reason = cells[1] if len(cells) > 1 else ""
            if subject in exemptions:
                errors.append(f"duplicate administrative exemption: {subject}")
            exemptions[subject] = reason
            if not reason:
                errors.append(f"administrative exemption has no reason: {subject}")

    if upstream_ref is not None and fork_subjects is not None:
        errors.append("pass upstream_ref or fork_subjects, not both")
    elif upstream_ref is not None:
        repo = path.resolve().parent
        try:
            fork_subjects = _fork_subjects(repo, upstream_ref)
        except subprocess.CalledProcessError as exc:
            errors.append(
                f"could not read fork history from {upstream_ref}: "
                f"git exited {exc.returncode}"
            )

    if fork_subjects is not None:
        coverage_label = f"{upstream_ref}..HEAD" if upstream_ref else "fork history"
        indexed_subjects = {
            subject for _patch_id, _status, subjects in rows for subject in subjects
        }
        for patch_id, _status, subjects in rows:
            for subject in subjects:
                if subject not in fork_subjects:
                    errors.append(
                        f"{patch_id} stable subject missing from "
                        f"{coverage_label}: {subject}"
                    )
        for subject in sorted(fork_subjects - indexed_subjects - set(exemptions)):
            errors.append(
                "fork-only subject is neither indexed nor exempt: " + subject
            )
        for subject in sorted(set(exemptions) - fork_subjects):
            errors.append(
                f"administrative exemption is not present in {coverage_label}: {subject}"
            )

    if history_baseline is not None:
        errors.extend(
            _validate_registration_history(path.resolve().parent, history_baseline)
        )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=Path("MAINTENANCE.md"),
    )
    parser.add_argument(
        "--upstream-ref",
        help="Also require every indexed stable subject in <ref>..HEAD history.",
    )
    parser.add_argument(
        "--history-baseline",
        help="Require every later commit to register its subject in that same commit.",
    )
    args = parser.parse_args()

    errors = validate_manifest(
        args.manifest,
        upstream_ref=args.upstream_ref,
        history_baseline=args.history_baseline,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"maintenance manifest valid: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
