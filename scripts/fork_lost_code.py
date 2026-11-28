#!/usr/bin/env python3
"""Audit fork-added lines across an upstream sync.

For every file the fork changed between the previous upstream base and the fork
main merged into this sync, collect the nonblank lines the fork added
(``git diff -U0``). A line is kept when normalized text exists in the sync HEAD at
its path, the upstream rename target, or anywhere in HEAD's tracked files. Whitespace,
Unicode/ASCII quotes and punctuation-only lines are normalized safely. Import-only lines
are ignored only when the imported name is still imported in the renamed target module.
Remaining lines need a reviewed mark.

FORK-RESOLUTION.md marks a group with a line of the form::

    - `<group-id>` upstream-equivalent: <replacement citation>
    - `<group-id>` moved: <path>
    - `<group-id>` intentionally dropped: <reason>

``<path>@*`` marks every group in a file and ``<path>@<symbol>::*`` every group
under one enclosing symbol.  ``--write`` regenerates the file, writes evidence
for exact moves itself (the cited HEAD paths), keeps every reviewed mark, and lists
every other group as ``UNMARKED``.  It never invents a reason.

A reviewed ``upstream-equivalent`` or ``intentionally dropped`` mark must cite
evidence: a ``path:line`` in HEAD, or a ``MAINTENANCE.md`` unit.  The old generic
"no exact replacement" default is rejected.  The script exits 1 while any group is
unmarked or carries an invalid mark.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterator

BASE = "ae2a81ccc00e"  # upstream v0.23.2, the fork's previous upstream base
FORK = "5b6bd5634ec25edb571edf736a689d459adc9c56"  # fork main merged by 5950fe30a8d
UPSTREAM_SYNC = "8fc0374447313e276f6b9a2764bfc750190c966a"  # upstream v0.23.4
RESOLUTION = "FORK-RESOLUTION.md"
CATEGORIES = ("upstream-equivalent", "moved", "intentionally dropped")
MARK = re.compile(
    r"^\s*[-*]\s+`([^`]+)`\s+(upstream-equivalent|moved|intentionally dropped)\s*:\s*(.*\S)?\s*$"
)
GENERATED = "<!-- generated-by: scripts/fork_lost_code.py -->"
MAX_BLOB = 2_000_000  # generated/vendored blobs above this are not indexed line by line
AUTO_DROPPED_REASON = "no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line"
_CITATION = re.compile(r"[\w./-]+\.\w+:\d+|MAINTENANCE\.md|maintenance/[\w.-]+\.md")


def valid_mark(category: str, text: str) -> bool:
    """A reviewed mark needs text, and equivalence or drop claims need a citation."""
    text = text.strip()
    if not text or text == AUTO_DROPPED_REASON:
        return False
    if category in {"upstream-equivalent", "intentionally dropped"}:
        return bool(_CITATION.search(text))
    return True
_IMPORT_FROM = re.compile(r"^\s*from\s+[^#]+?\s+import\s+(.+?)\s*(?:#.*)?$")
_IMPORT_PLAIN = re.compile(r"^\s*import\s+(.+?)\s*(?:#.*)?$")
_QUOTE_TRANSLATION = str.maketrans({
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
})


def normalize_line(line: str) -> str:
    """Normalize indentation and spacing outside quoted literals."""
    text = unicodedata.normalize("NFKC", line).translate(_QUOTE_TRANSLATION).strip()
    out: list[str] = []
    quote: str | None = None
    escaped = False
    space_pending = False
    for char in text:
        if quote:
            if space_pending:
                out.append(" ")
                space_pending = False
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {"'", '"'}:
            if space_pending:
                out.append(" ")
                space_pending = False
            quote = char
            out.append('"')
        elif char.isspace():
            space_pending = bool(out)
        else:
            if space_pending:
                out.append(" ")
                space_pending = False
            out.append(char)
    return "".join(out).strip()


def _imported_names(line: str) -> set[str]:
    match = _IMPORT_FROM.match(line) or _IMPORT_PLAIN.match(line)
    if not match:
        return set()
    names: set[str] = set()
    payload = match.group(1).strip().strip("()")
    for item in payload.split(","):
        item = item.strip()
        if not item or item == "*":
            continue
        item = item.split(" as ", 1)[-1].strip()
        if item:
            names.add(item)
    return names


def is_import_only(line: str) -> bool:
    return bool(_IMPORT_FROM.match(line) or _IMPORT_PLAIN.match(line))


def is_punctuation_only(line: str) -> bool:
    return not any(char.isalnum() or char == "_" for char in line)


def git_stream(root: Path, *args: str) -> Iterator[str]:
    proc = subprocess.Popen(
        ["git", *args], cwd=root, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    try:
        yield from proc.stdout
    finally:
        proc.stdout.close()
        if proc.wait() != 0:
            raise subprocess.CalledProcessError(proc.returncode, ["git", *args])


class Blobs:
    """One long-lived ``git cat-file --batch`` reader for HEAD blobs."""

    def __init__(self, root: Path) -> None:
        self.proc = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )

    def read(self, spec: str) -> bytes | None:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(spec.encode() + b"\n")
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            if len(header) == 3:  # tree/commit: drain it
                self.proc.stdout.read(int(header[2]) + 1)
            return None
        data = self.proc.stdout.read(int(header[2]))
        self.proc.stdout.read(1)
        return data

    def lines(self, spec: str) -> set[str]:
        data = self.read(spec)
        if data is None or b"\0" in data[:8000]:
            return set()
        return {normalized for ln in data.decode("utf-8", "replace").splitlines()
                if (normalized := normalize_line(ln))}

    def import_names(self, spec: str) -> set[str]:
        data = self.read(spec)
        if data is None or b"\0" in data[:8000]:
            return set()
        return {name for ln in data.decode("utf-8", "replace").splitlines()
                for name in _imported_names(ln)}

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait()


def rename_map(root: Path) -> dict[str, str]:
    """old -> new paths from upstream v0.23.2 to v0.23.4 with rename detection."""
    mapping: dict[str, str] = {}
    for raw in git_stream(root, "diff", "-M20%", "--name-status", f"{BASE}..{UPSTREAM_SYNC}"):
        fields = raw.rstrip("\n").split("\t")
        if fields[0].startswith("R") and len(fields) >= 3:
            mapping[fields[1]] = fields[2]
    return mapping


def fork_files(root: Path, base: str, fork: str) -> Iterator[tuple[str, list[dict]]]:
    """Stream the fork diff once, yielding (path, hunks) per file."""
    path: str | None = None
    hunks: list[dict] = []
    current: dict | None = None
    for raw in git_stream(root, "diff", "--no-ext-diff", "--no-color", "-U0", "--no-renames", f"{base}..{fork}"):
        line = raw.rstrip("\n")
        if line.startswith("diff --git "):
            if path and hunks:
                yield path, hunks
            path, hunks, current = None, [], None
        elif line.startswith("+++ ") and current is None:
            path = None if line == "+++ /dev/null" else line[6:]
        elif line.startswith("@@") and path:
            fields = line.split("@@", 2)
            context = fields[2].strip() if len(fields) == 3 else ""
            current = {"hunk": fields[1].strip(), "symbol": context[:80] or "(module)", "lines": []}
            hunks.append(current)
        elif current is not None and line.startswith("+"):
            current["lines"].append(line[1:])
    if path and hunks:
        yield path, hunks


def global_index(root: Path, blobs: Blobs, wanted: set[str]) -> dict[str, set[str]]:
    """Locate normalized wanted lines anywhere in HEAD (one pass, bounded memory)."""
    found: dict[str, set[str]] = defaultdict(set)
    if not wanted:
        return found
    for raw in git_stream(root, "ls-tree", "-r", "-l", "HEAD"):
        meta, _, path = raw.rstrip("\n").partition("\t")
        parts = meta.split()
        if len(parts) != 4 or parts[1] != "blob" or not parts[3].isdigit() or int(parts[3]) > MAX_BLOB:
            continue
        for line in blobs.lines(parts[2]):
            if line in wanted:
                found[line].add(path)
    return found


def is_test_or_doc(path: str) -> bool:
    return (path.startswith(("tests/", "website/", "docs/", "maintenance/")) or "/tests/" in path
            or path.endswith((".md", ".mdx", ".rst", ".txt")) or ".test." in path or ".spec." in path)


def audit(root: Path, base: str, fork: str) -> tuple[list[dict], dict[str, str], dict[str, int]]:
    renames = rename_map(root)
    blobs = Blobs(root)
    pending: list[tuple[str, dict, str, tuple[str, ...]]] = []
    stats = Counter()
    try:
        for path, hunks in fork_files(root, base, fork):
            candidates = tuple(dict.fromkeys((path, renames.get(path, path))))
            present: set[str] = set()
            target_imports: set[str] = set()
            for candidate in candidates:
                present |= blobs.lines(f"HEAD:{candidate}")
                target_imports |= blobs.import_names(f"HEAD:{candidate}")
            for hunk in hunks:
                for line in hunk["lines"]:
                    normalized = normalize_line(line)
                    if not normalized:
                        continue
                    stats["fork_lines"] += 1
                    if normalized in present:
                        stats["path_match"] += 1
                        continue
                    if is_punctuation_only(normalized):
                        stats["punctuation_ignored"] += 1
                        continue
                    if is_import_only(line) and (_imported_names(line) & target_imports):
                        stats["import_ignored"] += 1
                        continue
                    pending.append((path, hunk, normalized, candidates))
            del present, target_imports
        stats["after_path_and_filters"] = len(pending)
        index = global_index(root, blobs, {s for _, _, s, _ in pending})
    finally:
        blobs.close()

    for _, _, line, _ in pending:
        if index.get(line):
            stats["global_match"] += 1
    stats["after_global"] = len(pending) - stats["global_match"]

    grouped: dict[tuple[str, str, str], dict] = {}
    for path, hunk, line, candidates in pending:
        where = index.get(line, set()) - set(candidates)
        if where and not is_test_or_doc(path):
            # Runtime code is not "moved" into a test, fixture or doc that merely quotes it.
            runtime_where = {p for p in where if not is_test_or_doc(p)}
            if not runtime_where:
                stats["runtime_only_in_tests_or_docs"] += 1
            where = runtime_where
        status = "moved" if where else "missing"
        key = (path, hunk["hunk"], status)
        group = grouped.get(key)
        if group is None:
            group = grouped[key] = {
                "id": f"{path}@{hunk['hunk']}" + ("#moved" if where else ""),
                "path": path, "rename": candidates[-1] if len(candidates) > 1 else None,
                "hunk": hunk["hunk"], "symbol": hunk["symbol"], "status": status,
                "lines": [], "locations": set(),
            }
        group["lines"].append(line)
        group["locations"] |= where
    records = sorted(grouped.values(), key=lambda g: (g["path"], g["hunk"], g["status"]))
    for group in records:
        group["locations"] = sorted(group["locations"])
    stats["groups"] = len(records)
    stats["lines"] = sum(len(g["lines"]) for g in records)
    stats["missing_lines"] = sum(len(g["lines"]) for g in records if g["status"] == "missing")
    stats["missing_groups"] = sum(1 for g in records if g["status"] == "missing")
    return records, renames, dict(stats)


def read_marks(path: Path) -> dict[str, tuple[str, str]]:
    marks: dict[str, tuple[str, str]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = MARK.match(line)
            if m:
                text = (m.group(3) or "").strip()
                if valid_mark(m.group(2), text):
                    marks[m.group(1)] = (m.group(2), text)
    return marks


def mark_for(group: dict, marks: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    for key in (group["id"], f"{group['path']}@{group['symbol']}::*", f"{group['path']}@*"):
        if key in marks:
            return marks[key]
    if group["status"] == "moved":
        shown = ", ".join(f"`{p}`" for p in group["locations"][:4])
        more = len(group["locations"]) - 4
        return ("moved", shown + (f" (+{more} more)" if more > 0 else ""))
    return None


def render(records: list[dict], marks: dict[str, tuple[str, str]], preamble: str) -> str:
    out = [preamble.rstrip(), "", GENERATED, "", "## Items", ""]
    by_path: dict[str, list[dict]] = defaultdict(list)
    for g in records:
        by_path[g["path"]].append(g)
    big = len(records) > 300
    wildcards = {k: v for k, v in marks.items() if k.endswith("@*") or k.endswith("::*")}
    for path, groups in by_path.items():
        rename = groups[0]["rename"]
        out.append(f"### `{path}`" + (f" → `{rename}`" if rename else ""))
        out.append("")
        if f"{path}@*" in wildcards:
            cat, text = wildcards[f"{path}@*"]
            out.append(f"- `{path}@*` {cat}: {text}")
        symbols_done: set[str] = set()
        last = None
        for g in groups:
            if big and g["symbol"] != last:
                last = g["symbol"]
                out.append(f"- {last}")
                skey = f"{path}@{last}::*"
                if skey in wildcards and skey not in symbols_done:
                    cat, text = wildcards[skey]
                    out.append(f"  - `{skey}` {cat}: {text}")
                    symbols_done.add(skey)
            indent = "  " if big else ""
            sample = g["lines"][0][:90].rstrip().replace("`", "'")
            explicit = marks.get(g["id"])
            if explicit:
                out.append(f"{indent}- `{g['id']}` {explicit[0]}: {explicit[1]}")
            elif g["status"] == "moved":
                generated = mark_for(g, marks)
                assert generated is not None
                out.append(f"{indent}- `{g['id']}` {generated[0]}: {generated[1]}")
            elif not mark_for(g, marks):
                out.append(f"{indent}- `{g['id']}` UNMARKED ({len(g['lines'])} lines): {sample}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--fork", default=FORK)
    ap.add_argument("--resolution", default=RESOLUTION)
    ap.add_argument("--write", action="store_true", help="regenerate the resolution file, keeping reviewed marks")
    ap.add_argument("--json", help="write the raw groups as JSON to this path")
    ap.add_argument("--show-unmarked", type=int, default=20)
    args = ap.parse_args()
    root = Path(args.root).resolve()
    records, _, stats = audit(root, args.base, args.fork)
    res_path = root / args.resolution
    marks = read_marks(res_path)

    if args.json:
        Path(args.json).write_text(json.dumps(records, indent=1), encoding="utf-8")
    if args.write:
        text = res_path.read_text(encoding="utf-8") if res_path.exists() else ""
        preamble = text.split(GENERATED, 1)[0] if GENERATED in text else (
            "# Fork resolution\n\nLost-code audit for the v0.23.4 sync. Regenerate with "
            "`python scripts/fork_lost_code.py --write`; check with `python scripts/fork_lost_code.py`.\n"
        )
        res_path.write_text(render(records, marks, preamble), encoding="utf-8")
        marks = read_marks(res_path)

    counts: Counter[str] = Counter()
    line_counts: Counter[str] = Counter()
    unmarked = []
    for g in records:
        mark = mark_for(g, marks)
        if not mark or not mark[1]:
            unmarked.append(g)
            continue
        counts[mark[0]] += 1
        line_counts[mark[0]] += len(g["lines"])
    total = sum(len(g["lines"]) for g in records)
    print(f"matching steps (lines): fork additions {stats['fork_lines']} -> path/rename {stats['fork_lines'] - stats['path_match']} -> filters {stats['after_path_and_filters']} -> global HEAD {stats['after_global']} -> runtime-not-in-tests guard {stats['missing_lines']} ({stats['missing_groups']} groups)")
    print(f"  path/rename matches: {stats.get('path_match', 0)}; punctuation ignored: {stats.get('punctuation_ignored', 0)}; import-only ignored: {stats.get('import_ignored', 0)}; global matches: {stats.get('global_match', 0)} (runtime lines matched only in tests/docs, kept as candidates: {stats.get('runtime_only_in_tests_or_docs', 0)})")
    print(f"fork-added lines missing from their path: {total} in {len(records)} groups across {len({g['path'] for g in records})} files")
    for c in CATEGORIES:
        print(f"  {c}: {counts[c]} groups / {line_counts[c]} lines")
    print(f"  unmarked: {len(unmarked)} groups / {sum(len(g['lines']) for g in unmarked)} lines")
    for g in unmarked[: args.show_unmarked]:
        print(f"UNMARKED `{g['id']}` [{g['symbol']}] {len(g['lines'])} lines: {g['lines'][0][:100]}")
    return 1 if unmarked else 0


if __name__ == "__main__":
    sys.exit(main())
